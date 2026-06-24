#!/usr/bin/env python3
"""In-memory backtest harness — load bars ONCE, run many strategy variants fast,
print a ranked leaderboard. Default = 1-minute bars (fast exploration); set
TABLE=bars_1s to validate winners at tick resolution.

A "strategy" = an entry mask (built from precomputed features) + exit rules
(target%, stop%, max_hold bars). Add theories in build_strategies(); everything
else (entry → first-touch of target/stop within the session → P&L → metrics) is
shared, so each new idea is a few lines and the whole grid runs in seconds.

Env: DATABASE_URL, TABLE(=bars_1m), SIZE_USD(=1000), SLIP(=0.0003), MAX_HOLD(=300)
"""
import os
import numpy as np
import pandas as pd
import psycopg2

DB   = os.environ["DATABASE_URL"]
TBL  = os.environ.get("TABLE", "bars_1m")
SIZE = float(os.environ.get("SIZE_USD", "1000"))
SLIP = float(os.environ.get("SLIP", "0.0003"))
HOLD = int(os.environ.get("MAX_HOLD", "300"))


def load():
    # exclude inverse / short-vol ETFs — a long "buy the dip" strategy must not trade
    # instruments built to fall (they bleed structurally).
    con = psycopg2.connect(DB); cur = con.cursor()
    cur.execute(f"SELECT symbol, ts, high, low, close FROM {TBL} "
                f"WHERE is_rth AND symbol NOT IN ('SQQQ','SOXS','UVXY') ORDER BY symbol, ts")
    rows = cur.fetchall(); con.close()
    df = pd.DataFrame(rows, columns=["symbol", "ts", "high", "low", "close"])
    for c in ("high", "low", "close"):
        df[c] = df[c].astype(float)
    t = pd.to_datetime(df["ts"], utc=True).dt.tz_convert("America/New_York")
    df["session"] = t.dt.strftime("%Y%m%d").astype(int)   # NY trading day
    df["month"]   = t.dt.strftime("%Y-%m")
    return df


def add_features(g):
    c = g["close"]
    for p in (10, 20, 50):
        g[f"ma{p}"]  = c.rolling(p).mean()
        g[f"pma{p}"] = g[f"ma{p}"].shift(1)
    g["pclose"] = c.shift(1)
    g["min30"]  = g["low"].rolling(30).min()
    g["max30"]  = g["high"].rolling(30).max()
    # DAILY regime (stable for the whole day, no intraday flip, no lookahead):
    # was the PRIOR day's close above its 20-day average?
    dclose = g.groupby("session")["close"].last()
    dup = (dclose.shift(1) > dclose.rolling(60).mean().shift(1))   # ~3-month trend, prior day
    g["regup"] = g["session"].map(dup).fillna(False).to_numpy()
    return g


def sim(g, mask, target, stop, max_hold, short=False):
    """First-touch simulator, long OR short. Enter at the signal bar's close.
    target hit -> +target; stop hit -> -stop (stop wins ties); neither within
    max_hold (same session) -> exit at that bar's close. Slippage hurts both ends."""
    high = g["high"].to_numpy(); low = g["low"].to_numpy(); close = g["close"].to_numpy()
    sess = g["session"].to_numpy(); mon = g["month"].to_numpy()
    n = len(close)
    idx = np.flatnonzero(np.asarray(mask.fillna(False), bool))
    out = []
    for i in idx:
        if i + 1 >= n or sess[i + 1] != sess[i]:
            continue
        j = i + 1; lim = min(i + max_hold, n - 1); pnl = None
        if not short:                                   # LONG
            buy = close[i] * (1 + SLIP); tgt = buy * (1 + target); stp = buy * (1 - stop) if stop else None
            while j <= lim and sess[j] == sess[i]:
                if stp is not None and low[j] <= stp:
                    pnl = -stop; break
                if high[j] >= tgt:
                    pnl = target; break
                j += 1
            if pnl is None:
                k = j if (j <= lim and sess[j] == sess[i]) else j - 1
                pnl = (close[k] * (1 - SLIP) - buy) / buy
        else:                                           # SHORT (mirror)
            sell = close[i] * (1 - SLIP); tgt = sell * (1 - target); stp = sell * (1 + stop) if stop else None
            while j <= lim and sess[j] == sess[i]:
                if stp is not None and high[j] >= stp:
                    pnl = -stop; break
                if low[j] <= tgt:
                    pnl = target; break
                j += 1
            if pnl is None:
                k = j if (j <= lim and sess[j] == sess[i]) else j - 1
                pnl = (sell - close[k] * (1 + SLIP)) / sell
        out.append((mon[i], pnl))
    return out


def build_strategies():
    """name -> (entry_mask_fn(g) -> bool Series, target, stop|None, max_hold).
    Currently a grid over the mean-reversion bounce; add more here."""
    # PER-STOCK regime switch (direction from each stock's trailing ~3-month trend):
    #   uptrend  -> LONG the dip  (no stop)
    #   downtrend-> SHORT the rip (2% stop, which tamed the squeeze losses)
    # Each leg = (mask_fn, short?, stop). No lookahead: regup uses prior-day trend.
    long_leg  = (lambda g: (g["pclose"] <= g["pma10"]) & (g["close"] > g["ma10"]) &
                 (g["min30"] <= g["close"] * 0.98) & g["regup"], False, None)
    short_leg = (lambda g: (g["pclose"] >= g["pma10"]) & (g["close"] < g["ma10"]) &
                 (g["max30"] >= g["close"] * 1.02) & ~g["regup"], True, 0.02)
    return {"PER-STOCK regime (long-dip uptrend / short-rip+2%stop downtrend)":
            ([long_leg, short_leg], 0.02, 300)}


def metrics(trades):
    p = np.array([x[1] for x in trades], float)
    usd = p * SIZE
    cum = np.cumsum(usd)
    dd = (cum - np.maximum.accumulate(cum)).min() if len(cum) else 0.0
    sharpe = usd.mean() / usd.std() * np.sqrt(len(usd)) if len(usd) > 1 and usd.std() > 0 else 0.0
    return dict(trades=len(p), win=100 * (p > 0).mean(), avg=p.mean() * 100,
                total=usd.sum(), maxdd=dd, sharpe=sharpe)


def main():
    df = load()
    feats = {sym: add_features(g.reset_index(drop=True)) for sym, g in df.groupby("symbol")}
    strat = build_strategies()
    print(f"{TBL}: {len(df):,} bars · {len(feats)} symbols · {len(strat)} variants · "
          f"${SIZE:g}/trade · slip {SLIP*1e4:g}bps · hold {HOLD}\n", flush=True)

    rows = []
    for name, (legs, tgt, hold) in strat.items():
        trades = []
        for g in feats.values():
            for mfn, short, lstop in legs:
                trades += sim(g, mfn(g), tgt, lstop, hold, short)
        if trades:
            rows.append((name, metrics(trades)))
    rows.sort(key=lambda r: -r[1]["total"])

    hdr = f"{'strategy':<32}{'trades':>7}{'win%':>7}{'avg%':>8}{'total$':>9}{'maxDD$':>9}{'sharpe':>8}"
    print(hdr); print("-" * len(hdr))
    for name, m in rows:
        print(f"{name:<32}{m['trades']:>7}{m['win']:>7.1f}{m['avg']:>8.3f}"
              f"{m['total']:>9.0f}{m['maxdd']:>9.0f}{m['sharpe']:>8.2f}")

    # per-stock x per-month breakdown for the winner ($SIZE per stock)
    if rows:
        name = rows[0][0]; legs, tgt, hold = strat[name]
        recs = []
        for sym, g in feats.items():
            for mfn, short, lstop in legs:
                for m, p in sim(g, mfn(g), tgt, lstop, hold, short):
                    recs.append((sym, m, p * SIZE))
        bt = pd.DataFrame(recs, columns=["symbol", "month", "usd"])
        piv = bt.pivot_table(index="symbol", columns="month", values="usd",
                             aggfunc="sum").fillna(0).round(0).astype(int)
        piv["YEAR$"] = piv.sum(axis=1)
        piv["YEAR%"] = (piv["YEAR$"] / SIZE * 100).round(1)
        piv = piv.sort_values("YEAR$", ascending=False)
        pd.set_option("display.width", 400); pd.set_option("display.max_columns", 50)
        print(f"\n=== BEST: {name} — $ per stock per month (${SIZE:g} each) ===")
        print(piv.to_string())
        for sym in piv.index:                    # clean per-stock lines (easy to parse)
            print(f"PS {sym} {int(piv.loc[sym,'YEAR$'])} {piv.loc[sym,'YEAR%']}")
        n = len(piv); net = int(piv["YEAR$"].sum())
        print(f"\nPORTFOLIO: {n} stocks x ${SIZE:g} = ${n*SIZE:,.0f} deployed → "
              f"net ${net:,} ({net/(n*SIZE)*100:.1f}%)")


if __name__ == "__main__":
    main()
