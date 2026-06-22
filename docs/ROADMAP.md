# Roadmap

Phased so the riskiest operational piece (IBKR gateway/auth) is proven first, and **no real money** is touched until the full stack has run clean on paper for multiple sessions.

## Phase 0 — Scaffold + gateway connectivity (no trading)
- Clone the `stocks` deploy pattern (`scripts/deploy.sh`, `deploy/*.yaml`) into `nomad`, namespace `nomad`, Postgres up.
- Stand up `ibgw` (ib-gateway-docker, paper mode, port 4002) and prove `ib_insync` connects from a throwaway task: `reqCurrentTime`, `reqAccountSummary`, `reqPositions`.
- **Exit:** green connection + account summary in logs. De-risks the hardest part first.

## Phase 1 — Paper-trading MVP  *(scope is an open decision — see below)*
Full loop on the paper account: ingest → signals → risk gate → paper orders → monitoring UI + kill-switch.
1. Copy config + db layer; `broker.py` subscribes to IBKR ticks (`reqTickByTickData`) for a small fixed watchlist.
2. Write `app/strategy.py` — a NEW intraday rule (the chosen one), pure functions.
3. In-memory tick/bar feed from IBKR, market-hours-aware.
4. Risk manager: position-size, gross-exposure, max-positions, per-trade stop-loss, **max daily drawdown halt**, kill-switch.
5. Order router → IBKR paper, `order_ref` idempotency, persist-before-send.
6. Reconciler on connect + timer.
7. FastAPI read API + React UI: positions/PnL/orders/signals/heartbeat/gateway-status + kill/resume.
- **Exit:** runs unattended multiple sessions; kill-switch flattens within one loop; reconciler shows zero drift; every order traceable in Postgres.

## Phase 2 — Backtesting
- Reuse the *same* `strategy.py` driven by historical bars (IBKR historical, or massive/yahoo) through a simulated fill+risk harness. Assert paper and backtest produce identical signals on identical inputs.
- Add metrics: Sharpe, max DD, hit rate.

## Phase 3 — Strategy expansion + tuning
- UI-editable thresholds in `app_config` (mirrors `stocks` policy pattern; engine reloads each loop).
- Add intraday-specific rules (momentum / VWAP / ATR stops) beyond the swing-oriented ported rules.

## Phase 4 — Live cutover
- `TRADING_MODE=live`, port 4001, IB Key 2FA + IBC auto-restart through the nightly bounce.
- Tiny size caps to start; alerting (push/webhook) on halts, disconnects, drawdown breach.
- Explicit **arm-live** flag (separate from kill-switch) so live never auto-starts.

---

## Open decisions to resolve before P1 coding
1. **First-milestone scope:**
   - (a) Connect + monitor only — gateway up, stream prices, compute/log signals, show in UI, **no auto-orders**. Safest first step.
   - (b) Full auto-paper MVP — the complete loop above (paper money).
   - (c) Semi-auto — engine proposes, human approves each in UI.
2. **Which intraday strategy first** (the rules are new regardless — `stocks` rules are NOT ported):
   - (a) Opening-range breakout (ORB).
   - (b) VWAP reversion / trend.
   - (c) Momentum (e.g. rate-of-change + volume).
   - (d) One trivial rule (MA crossover on 1m bars) purely to validate the pipeline end-to-end first.
   - …plus the data resolution: trade off raw ticks vs 5s/1m bars built from ticks.
