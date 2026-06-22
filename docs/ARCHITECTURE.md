# Architecture

Recommended topology: **one image, two processes** (mirrors `stocks`' backend+collector split), a **separate IB Gateway**, **Postgres** for all state, and a React monitoring UI. No queue, no microservices — a single-user bot is simplest and safest as in-process asyncio with Postgres as the durability/cross-process boundary.

```
                 ┌─────────────────────────────────────────────┐
   FMP (HTTPS) ─▶ │ nomad-engine  (python -m app.engine)         │
   real-time     │   feed → strategy → RISK GATE → order router │──TCP──▶ ibgw:4002 ──▶ IBKR (paper)
   quotes        │   ↑ reconciler (on connect + timer)          │         (ib-gateway-docker + IBC)
                 └───────────────┬─────────────────────────────┘
                                 │ writes state (orders/fills/positions/pnl/signals)
                                 ▼
                          ┌────────────┐
                          │  Postgres  │  ◀── reads ──┐
                          └────────────┘              │
                                 ▲ kill-switch/config  │
                                 │                     │
                 ┌───────────────┴───────────┐   ┌─────┴───────────────┐
                 │ nomad-api (FastAPI :8080)  │◀──│ React UI (nginx)    │  host: nomad.securegion.com
                 │ read-only + kill/resume    │   │ /api/ → nomad-api   │
                 └────────────────────────────┘   └─────────────────────┘
```

## Components

### `nomad-engine` — the only thing that trades
Single asyncio loop (structural template: `stocks` `backend/app/collector.py`) hosting cooperating in-process tasks:
- **feed** (`app/feed.py`) — poll FMP `get_quote()` (real-time) into an in-memory `dict[ticker]→Quote`; optionally cross-check with IBKR `reqRealTimeBars`/`reqTickByTickData`. Market-hours-gated (reuse `collector._market_open`).
- **strategy** (`app/strategy.py`) — pure signal functions (ported from `stocks` `analysis.ts`) → intents `{ticker, side, strength}`.
- **risk** (`app/risk.py`) — MANDATORY gate every intent passes: position-size cap, gross-exposure cap, max open positions, per-trade stop-loss, **max daily drawdown halt**, kill-switch flag. The single chokepoint to money.
- **order router** (`app/broker.py`) — the only code calling `ib.placeOrder`. Client-generated `orderRef` idempotency key; persist order to Postgres **before** sending; handle fills/cancels.
- **reconciler** — on every (re)connect and on a timer, diff `ib.reqOpenOrders/reqPositions/reqExecutions` vs Postgres; surface drift; never blind-resend.
- Singleton: `replicas:1`, `strategy: Recreate`, fixed IBKR `clientId`. Treats disconnects as expected (backoff reconnect → reconcile → resume). Trades only when "connected AND market open AND outside the daily-restart window".

### `nomad-api` — read-only + control
FastAPI (`app/main.py`, :8080) serving the monitoring UI: positions, PnL, open/filled orders, signal log, engine heartbeat, gateway status. Hosts **kill-switch** (`POST /api/kill` / `/api/resume`) — flips a row in `app_config` that the engine polls each loop. **Never touches IBKR**; only reads/writes Postgres. Keeps the trading path isolated from web traffic.

### `ibgw` — IB Gateway (separate Deployment, NOT a sidecar)
`ib-gateway-docker` (gnzsnz) image bundling **IBC** for automated login + keepalive. `replicas:1`, `Recreate` (never two sessions on one account). Paper port **4002** (live 4001). ClusterIP `ibgw:4002`, no ingress. Secret `ibkr-creds`. Separate from the engine so gateway restarts/auth don't bounce the trading loop. **Paper = no 2FA** (runs unattended); live later needs IB Key + IBC auto-restart through the nightly bounce.

### Postgres — all state
New tables (extend the `stocks` `db.py` cache/config layer): `orders` (`order_ref` UNIQUE for idempotency, status, ts), `fills`, `positions`, `pnl_daily`, `signals`, `engine_heartbeat`; reuse `app_config` (JSONB) for kill-switch + risk params + an explicit **arm-live** flag.

## Communication
- engine ↔ api: **shared Postgres only** (no direct calls).
- engine ↔ IBKR: TCP via `ib_insync` (`ib.connectAsync('ibgw', 4002, clientId=1)`).
- engine ↔ FMP: outbound HTTPS (`httpx`).
- UI ↔ api: nginx reverse-proxies `/api/` (clone of `stocks` `nginx.conf`).

## Why not a queue / microservices
A single-user bot trading a few dozen symbols at seconds-to-minutes cadence is lower-latency and far easier to reason about for money-safety when one process owns order state in memory and persists every decision to Postgres. Kafka/Redis/NATS would add failure modes next to the money path for no benefit.
