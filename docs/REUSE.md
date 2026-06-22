# What to copy / port from `stocks`

Source repo: `/Users/master/Documents/projects/stocks` (same machine). Copy files in, then adapt names/namespace to `nomad`. Don't add a runtime dependency on `stocks` — copy, don't link.

## Copy as-is (low coupling — need only `config.settings` + `httpx`/`asyncpg`)
| From (`stocks/`) | Into (`nomad/`) | Notes |
|---|---|---|
| `backend/app/providers/base.py` | `backend/app/providers/base.py` | `Bar` dataclass + `MarketDataProvider` ABC |
| `backend/app/providers/fmp.py` | `backend/app/providers/fmp.py` | **`get_quote()` = real-time signal feed.** Also get_analyst/earnings/profile/income/balance/insider |
| `backend/app/providers/massive.py` | same | historical bars (`get_bars`), `get_snapshots` — for backtest/research |
| `backend/app/providers/yahoo.py` | same | deep-history backfill w/ retry + host rotation (Phase 2 backtest) |
| `backend/app/universe.py`, `sectors.py` | same | ticker universe / watchlist source |
| `backend/Dockerfile`, `frontend/Dockerfile`, `frontend/nginx.conf` | same | adapt image names/ports |
| `scripts/deploy.sh`, `deploy/*.yaml` | `scripts/`, `deploy/` | re-namespace to `nomad` (see below) |

## Adapt
- `backend/app/config.py` (pydantic BaseSettings) → keep `fmp_api_key`, `fmp_base_url`, `database_url`; **add** `ibgw_host`, `ibgw_port` (4002 paper), `ib_client_id`, `trading_mode`, and risk-limit defaults (max_position_usd, max_gross_usd, max_positions, stop_loss_pct, max_daily_drawdown_pct).
- `backend/app/db.py` → keep `cache_get/put`, `get_config/set_config`, the pool init + retry. **Replace** watchlist/favorites tables with nomad trading tables: `orders` (order_ref UNIQUE, status, ts), `fills`, `positions`, `pnl_daily`, `signals`, `engine_heartbeat`. Reuse `app_config` for kill-switch + risk params + arm-live flag.
- `backend/app/collector.py` → **structural template** for `backend/app/engine.py` (long-lived asyncio loop, market-hours gating via `_market_open`, per-task interval throttling, config reload each cycle).

## Port (TypeScript → Python, ~200 lines)
- `frontend/src/analysis.ts` → `backend/app/strategy.py`. Port `metrics()`, `CASE_RULES` (long+short, each `{id, side, param, def, run(metrics, v)}`), `runCases()`, `CASE_EXPLAIN`. Keep pure so backtest and live share one code path. (Fundamentals/swing-oriented — a baseline; add intraday rules later.)

## Write new
- `backend/app/engine.py` — orchestration loop (from `collector.py` template).
- `backend/app/broker.py` — `ib_insync` wrapper: connect/reconnect w/ backoff, `place_order` w/ `order_ref` idempotency, fill/exec callbacks, `reconcile()`.
- `backend/app/risk.py` — the risk gate.
- `backend/app/feed.py` — in-memory quote store fed by FMP (+ optional IBKR realtime bars).
- `backend/app/main.py` — FastAPI read API + `/api/kill`,`/api/resume`,`/api/state`,`/api/orders`,`/api/positions`,`/api/signals`,`/api/health`.
- `frontend/src/*` — trimmed React monitoring dashboard.
- `deploy/{namespace,postgres,ibgw,engine,api,frontend,ingress}.yaml`, `requirements.txt` (add `ib_insync`).

## Deploy pattern (clone of `stocks`)
`scripts/deploy.sh` with `NS=nomad`, `REMOTE_DIR=/root/nomad`, node `root@skynet1`: rsync → `docker build` `nomad-engine:latest` (from `./backend`) + `nomad-frontend:latest` (from `./frontend`) → `docker save | k3s ctr images import -` → create secrets (`nomad-db`, `fmp-api`, `ibkr-creds`) from `.env` → `kubectl apply` manifests → rollout restart `nomad-engine`, `nomad-api`, `nomad-frontend` (+ `ibgw` when its config changes). Ingress host `nomad.securegion.com`, traefik + cert-manager `letsencrypt-prod`, entrypoints `web,websecure`, behind Cloudflare. Only the UI is exposed; `ibgw` and `nomad-engine` are internal.
