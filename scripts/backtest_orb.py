#!/usr/bin/env python3
"""Opening-Range Breakout backtest over 1-second bars in Postgres.

Resamples bars_1s -> 1-minute (in SQL), filters to US regular hours (DST-aware
via zoneinfo), and runs a simple ORB per session:

  * Opening range  = high/low of the first OR_MIN minutes after 09:30 ET.
  * Entry          = first 1-min close to break OR high (long) or OR low (short),
                     one trade per day.
  * Stop           = opposite side of the opening range  (defines 1R of risk).
  * Target         = entry +/- TARGET_R * risk.
  * Else flat at FLAT_HH:FLAT_MM (force exit), and never hold overnight.

Position sized to risk RISK_USD per trade, so PnL is reported in $ and in R.
Costs: per-share commission + per-side slippage (conservative: stop/target/eod
fills take slippage against us). This logic is the seed of backend/app/strategy.py.

Env: DATABASE_URL, SYMBOL(=NVDA), OR_MIN(=15), TARGET_R(=2.0), RISK_USD(=100),
     SLIPPAGE(=0.02), COMMISSION(=0.0035), FLAT_HHMM(=1555)
"""
import asyncio
import datetime as dt
import os
from collections import defaultdict
from zoneinfo import ZoneInfo

import asyncpg

NY = ZoneInfo("America/New_York")
SYMBOL = os.environ.get("SYMBOL", "NVDA").upper()
OR_MIN = int(os.environ.get("OR_MIN", "15"))
TARGET_R = float(os.environ.get("TARGET_R", "2.0"))
RISK_USD = float(os.environ.get("RISK_USD", "100"))
SLIP = float(os.environ.get("SLIPPAGE", "0.02"))
COMM = float(os.environ.get("COMMISSION", "0.0035"))
FLAT = int(os.environ.get("FLAT_HHMM", "1555"))
OPEN_MIN = 9 * 60 + 30          # 09:30 ET in minutes-of-day
CLOSE_MIN = 16 * 60            # 16:00 ET
FLAT_MIN = (FLAT // 100) * 60 + (FLAT % 100)

RESAMPLE = """
SELECT date_trunc('minute', ts) AS m,
       (array_agg(open  ORDER BY ts ASC))[1]  AS o,
       max(high) AS h, min(low) AS l,
       (array_agg(close ORDER BY ts DESC))[1] AS c,
       sum(volume) AS v
FROM bars_1s WHERE symbol = $1 GROUP BY 1 ORDER BY 1;
"""


def run_session(bars):
    """bars: list of (minute_of_day, o,h,l,c) for ONE session, sorted. -> trade dict|None"""
    orb = [b for b in bars if OPEN_MIN <= b[0] < OPEN_MIN + OR_MIN]
    if len(orb) < max(2, OR_MIN // 2):
        return None                              # not enough opening data
    or_hi = max(b[2] for b in orb)
    or_lo = min(b[3] for b in orb)
    if or_hi <= or_lo:
        return None

    after = [b for b in bars if OPEN_MIN + OR_MIN <= b[0] < FLAT_MIN]
    entry = side = None
    for i, b in enumerate(after):
        if b[4] > or_hi:
            side, entry, idx = "long", b[4] + SLIP, i
            break
        if b[4] < or_lo:
            side, entry, idx = "short", b[4] - SLIP, i
            break
    if entry is None:
        return None

    if side == "long":
        stop, risk = or_lo, entry - or_lo
        target = entry + TARGET_R * risk
    else:
        stop, risk = or_hi, or_hi - entry
        target = entry - TARGET_R * risk
    if risk <= 0:
        return None
    shares = RISK_USD / risk

    exit_px = exit_reason = None
    for b in after[idx + 1:]:
        _, o, h, l, c = b
        if side == "long":
            if l <= stop:   exit_px, exit_reason = stop - SLIP, "stop";   break
            if h >= target: exit_px, exit_reason = target - SLIP, "target"; break
        else:
            if h >= stop:   exit_px, exit_reason = stop + SLIP, "stop";   break
            if l <= target: exit_px, exit_reason = target + SLIP, "target"; break
    if exit_px is None:                          # force flat at session end
        exit_px = (after[-1][4] - SLIP) if side == "long" else (after[-1][4] + SLIP)
        exit_reason = "eod"

    gross = (exit_px - entry) * shares if side == "long" else (entry - exit_px) * shares
    pnl = gross - COMM * shares * 2
    return {"side": side, "entry": round(entry, 2), "exit": round(exit_px, 2),
            "reason": exit_reason, "shares": round(shares, 1),
            "pnl": pnl, "R": pnl / RISK_USD}


async def main():
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    rows = await conn.fetch(RESAMPLE, SYMBOL)
    await conn.close()
    print(f"{SYMBOL}: {len(rows):,} 1-min bars resampled", flush=True)

    sessions = defaultdict(list)
    for r in rows:
        t = r["m"].astimezone(NY)
        mod = t.hour * 60 + t.minute
        if OPEN_MIN <= mod < CLOSE_MIN:          # regular hours only
            sessions[t.date()].append((mod, float(r["o"]), float(r["h"]),
                                       float(r["l"]), float(r["c"])))

    trades = []
    for day in sorted(sessions):
        bars = sorted(sessions[day])
        t = run_session(bars)
        if t:
            t["day"] = day
            trades.append(t)

    if not trades:
        print("no trades"); return
    n = len(trades)
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    tot = sum(t["pnl"] for t in trades)
    gw = sum(t["pnl"] for t in wins); gl = -sum(t["pnl"] for t in losses)
    # equity curve + max drawdown
    eq = 0.0; peak = 0.0; mdd = 0.0
    for t in trades:
        eq += t["pnl"]; peak = max(peak, eq); mdd = min(mdd, eq - peak)

    print(f"\n=== ORB backtest: {SYMBOL}  (OR={OR_MIN}m, target={TARGET_R}R, "
          f"risk=${RISK_USD:.0f}/trade) ===", flush=True)
    print(f"sessions traded : {n} of {len(sessions)} days")
    print(f"win rate        : {len(wins)/n*100:.1f}%  ({len(wins)}W / {len(losses)}L)")
    print(f"total P&L       : ${tot:,.0f}   ({tot/RISK_USD:+.1f}R)")
    print(f"avg / trade     : ${tot/n:,.1f}   ({tot/n/RISK_USD:+.2f}R)")
    print(f"profit factor   : {gw/gl:.2f}" if gl else "profit factor   : inf")
    print(f"max drawdown    : ${mdd:,.0f}   ({mdd/RISK_USD:.1f}R)")
    print(f"best / worst    : {max(t['R'] for t in trades):+.1f}R / "
          f"{min(t['R'] for t in trades):+.1f}R")
    by = defaultdict(float)
    for t in trades:
        by[t["reason"]] += 1
    print(f"exits           : " + ", ".join(f"{k}={int(v)}" for k, v in sorted(by.items())))
    print("\nlast 8 trades:")
    for t in trades[-8:]:
        print(f"  {t['day']} {t['side']:5} entry {t['entry']:>8} exit {t['exit']:>8} "
              f"{t['reason']:6} {t['R']:+.2f}R  ${t['pnl']:+.0f}")


if __name__ == "__main__":
    asyncio.run(main())
