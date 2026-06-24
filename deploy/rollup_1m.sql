-- 1-minute rollup of bars_1s, for fast chart/API queries (raw 1s stays the
-- source of truth). Re-runnable: rebuilds the table from scratch.
CREATE TABLE IF NOT EXISTS bars_1m (
  symbol text   NOT NULL,
  ts     timestamptz NOT NULL,
  open   float8, high float8, low float8, close float8, volume float8,
  PRIMARY KEY (symbol, ts)
);
TRUNCATE bars_1m;
INSERT INTO bars_1m
  SELECT symbol,
         date_trunc('minute', ts) AS ts,
         (array_agg(open  ORDER BY ts))[1]  AS open,
         max(high) AS high,
         min(low)  AS low,
         (array_agg(close ORDER BY ts DESC))[1] AS close,
         sum(volume) AS volume
  FROM bars_1s
  GROUP BY symbol, date_trunc('minute', ts);
