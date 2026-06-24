#!/usr/bin/env python3
"""Backfill 1-MINUTE bars from massive into bars_1m for a universe of symbols.
Pages in ~30-day chunks (a month of 1-min is well under the 50k row cap), tags
is_rth (DST-aware), and COPYs in. Idempotent per symbol (clears then reloads).

Env: MASSIVE_API_KEY, MASSIVE_BASE_URL, DATABASE_URL, SYMBOLS(comma), DAYS(=365)
"""
import asyncio
import datetime as dt
import os
from zoneinfo import ZoneInfo

import asyncpg
import httpx

NY   = ZoneInfo("America/New_York")
BASE = os.environ.get("MASSIVE_BASE_URL", "https://api.massive.com").rstrip("/")
KEY  = os.environ["MASSIVE_API_KEY"]
DB   = os.environ["DATABASE_URL"]
SYMS = [s.strip().upper() for s in os.environ["SYMBOLS"].split(",") if s.strip()]
TBL  = os.environ.get("TABLE", "bars_1m")
OPEN, CLOSE = dt.time(9, 30), dt.time(16, 0)

DDL = f"""CREATE TABLE IF NOT EXISTS {TBL} (
  symbol text NOT NULL, ts timestamptz NOT NULL,
  open float8, high float8, low float8, close float8, volume float8,
  is_rth boolean, PRIMARY KEY (symbol, ts));"""
COLS = ["symbol", "ts", "open", "high", "low", "close", "volume", "is_rth"]


def chunks(step=30):
    # explicit START/END (ISO) if given, else last DAYS from today
    if os.environ.get("START") and os.environ.get("END"):
        start = dt.date.fromisoformat(os.environ["START"]); end = dt.date.fromisoformat(os.environ["END"])
    else:
        end = dt.date.today(); start = end - dt.timedelta(days=int(os.environ.get("DAYS", "365")))
    out, c = [], start
    while c <= end:
        out.append((c.isoformat(), min(c + dt.timedelta(days=step - 1), end).isoformat()))
        c += dt.timedelta(days=step)
    return out


async def fetch(client, sym, frm, to):
    url = f"{BASE}/v2/aggs/ticker/{sym}/range/1/minute/{frm}/{to}"
    params = {"adjusted": "true", "sort": "asc", "limit": 50000, "apiKey": KEY}
    for attempt in range(6):
        try:
            r = await client.get(url, params=params, timeout=60)
        except httpx.HTTPError:
            await asyncio.sleep(1.5 * (attempt + 1)); continue
        if r.status_code == 429:
            await asyncio.sleep(2.0 * (attempt + 1)); continue
        r.raise_for_status()
        d = r.json()
        if d.get("status") == "NOT_AUTHORIZED":
            return []
        rows = d.get("results") or []
        if len(rows) >= 50000:
            print(f"  !! {sym} {frm}: hit 50k cap", flush=True)
        out = []
        for x in rows:
            ts = dt.datetime.fromtimestamp(x["t"] / 1000, tz=dt.timezone.utc)
            t = ts.astimezone(NY).time()
            out.append((sym, ts, x.get("o"), x.get("h"), x.get("l"), x.get("c"),
                        x.get("v"), OPEN <= t < CLOSE))
        return out
    return []


async def main():
    conn = await asyncpg.connect(DB)
    await conn.execute(DDL)
    spans = chunks()
    print(f"{TBL}: {len(SYMS)} symbols, {spans[0][0]}..{spans[-1][1]}, {len(spans)} chunks each", flush=True)
    async with httpx.AsyncClient() as client:
        for sym in SYMS:
            await conn.execute(f"DELETE FROM {TBL} WHERE symbol=$1", sym)
            recs = []
            for frm, to in spans:
                recs += await fetch(client, sym, frm, to)
                await asyncio.sleep(0.1)
            if recs:
                await conn.copy_records_to_table(TBL, records=recs, columns=COLS)
            print(f"  ✓ {sym}: {len(recs):,} rows", flush=True)
    n = await conn.fetchval(f"SELECT count(DISTINCT symbol) FROM {TBL}")
    tot = await conn.fetchval(f"SELECT count(*) FROM {TBL}")
    print(f"done. {TBL}: {tot:,} rows across {n} symbols", flush=True)
    await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
