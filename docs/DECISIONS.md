# Decisions & rationale

### Separate project from `stocks`
`stocks` is a batch, read-only research dashboard (collector → Postgres → UI, ~30-min refresh). nomad is event-driven, stateful, latency-sensitive, and executes real money. Opposite requirements → separate repo, namespace, DB, secrets, deploy, ingress. Shared only: the *deploy pattern* and *copied source*. Zero shared runtime.

### Broker = Interactive Brokers (not Alpaca)
The user **already has funds in IBKR**, so no need to move money. IBKR is more capable than Alpaca (stocks, options, futures, FX, global) but operationally heavier: the API talks to **IB Gateway**, a desktop/headless app that must stay logged in. Use **`ib_insync`** (the de-facto Python algo lib) + a headless gateway automated by **IBC** (`ib-gateway-docker`).
- Alpaca was considered (cloud-native API, no gateway, free paper + IEX real-time) but rejected because funds are in IBKR.

### Paper trading first
IBKR **paper accounts have no 2FA** → the gateway runs unattended indefinitely. This lets us validate the entire stack (connect, data, signals, risk, orders, reconciliation, UI) with zero money and without the 2FA problem. Live cutover is a later phase: `TRADING_MODE=live`, port 4001, IB Key 2FA, IBC auto-restart, plus an explicit **arm-live** flag so live never starts by accident.

### Data: real-time ticks via IBKR (NOT FMP)
nomad is a **tick-driven intraday** trader, so the live feed must be a true **streaming tick socket** — not REST polling. **FMP only offers a REST quote (no tick WebSocket)**, so it is *not* the live feed. The clean source is **IBKR's own market data via `ib_insync`** — `reqTickByTickData` (every trade/quote) and `reqRealTimeBars` (5s) — streamed over the **same gateway connection used for execution**. One connection, real ticks, no second vendor.
- Requires IBKR **market-data subscriptions** (a few $/mo per bundle; line limits apply — fine for a focused watchlist).
- **FMP / massive are demoted to OPTIONAL**: fundamentals/reference filters and historical bars for backtesting only. Not on the live trading path.
- If broader/cheaper tick coverage is ever needed: Polygon real-time or Alpaca SIP WebSocket. But default to IBKR ticks to avoid a second integration.

### Money-safety requirements (from the first order-placing build)
- One **risk gate** every order passes through; engine is a strict **singleton**.
- Hard caps: per-symbol size, gross exposure, max open positions.
- Per-trade **stop-loss**; **max daily drawdown auto-halt**.
- Manual **kill-switch** that flattens + stops within one loop.
- **Idempotent orders**: client-generated `order_ref`, persisted (UNIQUE) before `placeOrder`; on reconnect, **reconcile** vs `reqOpenOrders`/`reqExecutions` — never blind-resend.
- Separate explicit **arm-live** flag, distinct from the kill-switch.

### Topology: monolith-ish, not microservices
Single-user bot → one engine process (in-process asyncio tasks) + a read-only API + the gateway + Postgres. No message broker. Simplicity = safety near the money path.

### Caveats / known risks
- IBKR gateway session management (daily restart, live 2FA) is the main operational friction — mitigated by paper-first + IBC.
- Retail data/broker latency suits second/minute strategies, not microsecond HFT.
### Strategy: clean new intraday rules (do NOT port the `stocks` rules)
The `stocks` `analysis.ts` rules are fundamentals/**swing**-oriented and are the wrong shape for intraday — they will **not** be ported as the strategy. nomad gets a **fresh, purpose-built intraday rule engine** (price-action: momentum/VWAP/ORB/mean-reversion + ATR-based stops, designed for tick/bar data). `analysis.ts` is at most a structural reference for how to express tunable rules — not a source of trading logic.
- Mind PDT rules and IBKR terms before live.
