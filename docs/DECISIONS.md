# Decisions & rationale

### Separate project from `stocks`
`stocks` is a batch, read-only research dashboard (collector → Postgres → UI, ~30-min refresh). nomad is event-driven, stateful, latency-sensitive, and executes real money. Opposite requirements → separate repo, namespace, DB, secrets, deploy, ingress. Shared only: the *deploy pattern* and *copied source*. Zero shared runtime.

### Broker = Interactive Brokers (not Alpaca)
The user **already has funds in IBKR**, so no need to move money. IBKR is more capable than Alpaca (stocks, options, futures, FX, global) but operationally heavier: the API talks to **IB Gateway**, a desktop/headless app that must stay logged in. Use **`ib_insync`** (the de-facto Python algo lib) + a headless gateway automated by **IBC** (`ib-gateway-docker`).
- Alpaca was considered (cloud-native API, no gateway, free paper + IEX real-time) but rejected because funds are in IBKR.

### Paper trading first
IBKR **paper accounts have no 2FA** → the gateway runs unattended indefinitely. This lets us validate the entire stack (connect, data, signals, risk, orders, reconciliation, UI) with zero money and without the 2FA problem. Live cutover is a later phase: `TRADING_MODE=live`, port 4001, IB Key 2FA, IBC auto-restart, plus an explicit **arm-live** flag so live never starts by accident.

### Data: FMP real-time for signals; massive delayed for research only
Verified in `stocks`: FMP `/stable/quote` is **real-time** (timestamp = now), massive.com (Polygon whitelabel, Starter plan) is **15-min delayed**. So nomad uses FMP for live signals. IBKR market data (`reqTickByTickData`/`reqRealTimeBars`, needs paid subscriptions) is the execution-side feed for fill/last-price sanity. Keep data and execution decoupled.
- **Future real path to true ticks:** IBKR data subscriptions, or upgrade massive/Polygon to a real-time tier, or Alpaca SIP. A WebSocket stream (push) would also be *fewer* API calls than polling.

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
- The ported `stocks` rules are fundamentals/**swing**-oriented; true intraday rules (momentum/VWAP/ATR) come later. Port them first only as a known-good, backtestable baseline.
- Mind PDT rules and IBKR terms before live.
