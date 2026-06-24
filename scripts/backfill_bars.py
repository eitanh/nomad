#!/usr/bin/env python3
"""Backfill 1-second OHLCV bars from massive (Polygon-compatible) into Postgres.

Pulls per trading day (a 1-second day is ~23k rows, well under the 50k cap, so no
pagination needed), COPYs into bars_1s. Idempotent per symbol: clears the symbol's
[start,end] range first, then re-loads — safe to re-run / resume.

Env:
  MASSIVE_API_KEY, MASSIVE_BASE_URL (default https://api.massive.com)
  DATABASE_URL
  SYMBOLS   comma-separated (default "NVDA,TSLA,TQQQ,SPY")
  DAYS      calendar days of history (default 365)
"""
import asyncio
import datetime as dt
import os

import asyncpg
import httpx

BASE = os.environ.get("MASSIVE_BASE_URL", "https://api.massive.com").rstrip("/")
KEY = os.environ["MASSIVE_API_KEY"]
DB = os.environ["DATABASE_URL"]
SYMBOLS = [s.strip().upper() for s in os.environ.get("SYMBOLS", "NVDA,TSLA,TQQQ,SPY").split(",") if s.strip()]
DAYS = int(os.environ.get("DAYS", "365"))

DDL = """
CREATE TABLE IF NOT EXISTS bars_1s (
    symbol  text             NOT NULL,
    ts      timestamptz      NOT NULL,
    open    double precision NOT NULL,
    high    double precision NOT NULL,
    low     double precision NOT NULL,
    close   double precision NOT NULL,
    volume  double precision NOT NULL,
    vwap    double precision,
    trades  integer,
    PRIMARY KEY (symbol, ts)
);
"""
COLS = ["symbol", "ts", "open", "high", "low", "close", "volume", "vwap", "trades"]


async def fetch_day(client: httpx.AsyncClient, sym: str, day: str) -> list[tuple]:
    url = f"{BASE}/v2/aggs/ticker/{sym}/range/1/second/{day}/{day}"
    params = {"adjusted": "true", "sort": "asc", "limit": 50000, "apiKey": KEY}
    for attempt in range(6):
        try:
            r = await client.get(url, params=params, timeout=60)
        except httpx.HTTPError as e:
            await asyncio.sleep(1.5 * (attempt + 1))
            if attempt == 5:
                raise
            continue
        if r.status_code == 429:                      # rate limited — back off
            await asyncio.sleep(2.0 * (attempt + 1))
            continue
        r.raise_for_status()
        data = r.json()
        if data.get("status") == "NOT_AUTHORIZED":
            print(f"    {sym} {day}: NOT_AUTHORIZED (outside plan window) — skipping")
            return []
        rows = data.get("results") or []
        if len(rows) >= 50000:
            print(f"    !! {sym} {day}: hit 50k cap — day truncated, needs pagination")
        out = []
        for x in rows:
            ts = dt.datetime.fromtimestamp(x["t"] / 1000, tz=dt.timezone.utc)
            out.append((sym, ts, x.get("o", 0.0), x.get("h", 0.0), x.get("l", 0.0),
                        x.get("c", 0.0), x.get("v", 0.0), x.get("vw"), x.get("n")))
        return out
    return []


async def main() -> None:
    end = dt.date.today()
    start = end - dt.timedelta(days=DAYS)
    print(f"backfill 1s bars: {SYMBOLS} from {start} to {end}")

    conn = await asyncpg.connect(DB)
    await conn.execute(DDL)

    async with httpx.AsyncClient() as client:
        for sym in SYMBOLS:
            await conn.execute(
                "DELETE FROM bars_1s WHERE symbol=$1 AND ts >= $2 AND ts < $3",
                sym, dt.datetime(start.year, start.month, start.day, tzinfo=dt.timezone.utc),
                dt.datetime(end.year, end.month, end.day, tzinfo=dt.timezone.utc) + dt.timedelta(days=1),
            )
            total, day = 0, start
            while day <= end:
                if day.weekday() < 5:                 # skip weekends
                    recs = await fetch_day(client, sym, day.isoformat())
                    if recs:
                        await conn.copy_records_to_table("bars_1s", records=recs, columns=COLS)
                        total += len(recs)
                    await asyncio.sleep(0.15)          # be polite to the API
                day += dt.timedelta(days=1)
            print(f"  ✓ {sym}: {total:,} rows loaded")

    n = await conn.fetchval("SELECT count(*) FROM bars_1s")
    print(f"done. bars_1s now holds {n:,} rows total.")
    await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
