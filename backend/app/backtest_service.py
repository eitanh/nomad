"""Resident in-RAM backtest service. Loads bars_1m ONCE at startup, holds the
featured per-symbol frames in memory, and runs strategy grids on demand in
~milliseconds — no pip, no reload, no cold cache. Internal only (ClusterIP).

  GET /health
  GET /grid?ma=10,20&dip=0.02,0.03&target=0.005,0.01&stop=none,0.02&hold=60,300
       &size=1000&top=25
       -> ranked leaderboard (JSON) for the mean-reversion "bounce" family.
"""
from contextlib import asynccontextmanager

import numpy as np
import pandas as pd
import psycopg2
from fastapi import FastAPI
from fastapi.responses import ORJSONResponse

from .config import settings

FEATS: dict = {}     # symbol -> featured DataFrame, held resident in RAM


INVERSE = ("SQQQ", "SOXS", "UVXY")           # never buy-the-dip a short/inverse instrument


def _load():
    con = psycopg2.connect(settings.database_url); cur = con.cursor()
    cur.execute("SELECT symbol, ts, high, low, close FROM bars_1m "
                "WHERE is_rth AND symbol NOT IN %s ORDER BY symbol, ts", (INVERSE,))
    rows = cur.fetchall(); con.close()
    df = pd.DataFrame(rows, columns=["symbol", "ts", "high", "low", "close"])
    for c in ("high", "low", "close"):
        df[c] = df[c].astype(float)
    t = pd.to_datetime(df["ts"], utc=True).dt.tz_convert("America/New_York")
    df["session"] = t.dt.strftime("%Y%m%d").astype(int)
    df["month"] = t.dt.strftime("%Y-%m")
    out = {}
    for sym, g in df.groupby("symbol"):
        g = g.reset_index(drop=True); c = g["close"]
        for p in (10, 20, 50):
            g[f"ma{p}"] = c.rolling(p).mean()
            g[f"pma{p}"] = g[f"ma{p}"].shift(1)
        g["pclose"] = c.shift(1)
        g["min30"] = g["low"].rolling(30).min()
        out[sym] = g
    return out


@asynccontextmanager
async def lifespan(_: FastAPI):
    global FEATS
    FEATS = _load()
    yield


app = FastAPI(title="nomad-backtest", lifespan=lifespan, default_response_class=ORJSONResponse)


def _sim(g, mask, target, stop, max_hold, slip):
    high = g["high"].to_numpy(); low = g["low"].to_numpy(); close = g["close"].to_numpy()
    sess = g["session"].to_numpy(); mon = g["month"].to_numpy()
    n = len(close)
    idx = np.flatnonzero(np.asarray(mask.fillna(False), bool))
    out = []
    for i in idx:
        if i + 1 >= n or sess[i + 1] != sess[i]:
            continue
        buy = close[i] * (1 + slip); tgt = buy * (1 + target); stp = buy * (1 - stop) if stop else None
        pnl = None; j = i + 1; lim = min(i + max_hold, n - 1)
        while j <= lim and sess[j] == sess[i]:
            if stp is not None and low[j] <= stp:
                pnl = -stop; break
            if high[j] >= tgt:
                pnl = target; break
            j += 1
        if pnl is None:
            k = j if (j <= lim and sess[j] == sess[i]) else j - 1
            pnl = (close[k] * (1 - slip) - buy) / buy
        out.append((mon[i], pnl))
    return out


@app.get("/health")
def health():
    return {"ok": True, "symbols": len(FEATS),
            "bars": int(sum(len(g) for g in FEATS.values()))}


@app.get("/grid")
def grid(ma: str = "10,20", dip: str = "0.02,0.03", target: str = "0.005,0.01",
         stop: str = "none,0.02", hold: str = "60,300",
         size: float = 1000.0, slip: float = 0.0003, top: int = 25):
    """Mean-reversion bounce family: enter when close crosses above MA(ma) after a
    >=dip selloff (lowest low of last 30 bars), exit at +target / -stop / max_hold."""
    mas = [int(x) for x in ma.split(",")]
    dips = [float(x) for x in dip.split(",")]
    tgts = [float(x) for x in target.split(",")]
    stops = [None if x.strip() == "none" else float(x) for x in stop.split(",")]
    holds = [int(x) for x in hold.split(",")]
    res = []
    for m in mas:
        for d in dips:
            for tg in tgts:
                for st in stops:
                    for h in holds:
                        trades = []
                        for g in FEATS.values():
                            mask = ((g["pclose"] <= g[f"pma{m}"]) & (g["close"] > g[f"ma{m}"]) &
                                    (g["min30"] <= g["close"] * (1 - d)))
                            trades += _sim(g, mask, tg, st, h, slip)
                        if not trades:
                            continue
                        p = np.array([x[1] for x in trades]); usd = p * size
                        cum = np.cumsum(usd)
                        dd = float((cum - np.maximum.accumulate(cum)).min()) if len(cum) else 0.0
                        sh = float(usd.mean() / usd.std() * np.sqrt(len(usd))) if len(usd) > 1 and usd.std() > 0 else 0.0
                        slbl = "n" if st is None else f"{st*100:g}"
                        res.append({"name": f"ma{m} dip>{d*100:g}% t{tg*100:g}% s{slbl} h{h}",
                                    "trades": len(p), "win": round(100*(p > 0).mean(), 1),
                                    "avg": round(p.mean()*100, 3), "total": round(usd.sum()),
                                    "maxdd": round(dd), "sharpe": round(sh, 2)})
    res.sort(key=lambda r: -r["total"])
    return {"variants": len(res), "results": res[:int(top)]}


@app.get("/detail")
def detail(ma: int = 10, dip: float = 0.02, target: float = 0.02,
           stop: str = "none", hold: int = 300, size: float = 1000.0, slip: float = 0.0003):
    """One strategy, broken out per stock ($size each) — sorted by net $."""
    st = None if stop.strip() == "none" else float(stop)
    rows = []
    for sym, g in FEATS.items():
        mask = ((g["pclose"] <= g[f"pma{ma}"]) & (g["close"] > g[f"ma{ma}"]) &
                (g["min30"] <= g["close"] * (1 - dip)))
        tr = _sim(g, mask, target, st, hold, slip)
        if not tr:
            continue
        usd = sum(p for _, p in tr) * size
        rows.append({"symbol": sym, "trades": len(tr), "usd": round(usd), "pct": round(usd / size * 100, 1)})
    rows.sort(key=lambda r: -r["usd"])
    net = sum(r["usd"] for r in rows)
    deployed = len(rows) * size
    return {"strategy": f"ma{ma} dip>{dip*100:g}% +{target*100:g}% stop{stop} hold{hold}",
            "stocks": len(rows), "deployed": deployed, "net": round(net),
            "pct": round(net / deployed * 100, 1) if deployed else 0, "per_stock": rows}
