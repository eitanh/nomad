# nomad

A **real-time intraday auto-trading** application for US equities, built around **Interactive Brokers** execution.

**Status:** scaffolding / planning. No trading code yet — this initial commit seeds the project context so work can begin in a fresh session. Read `CLAUDE.md` and `docs/` first.

## What this is (and is NOT)
- **Is:** an always-on, event-driven trading engine — ingest real-time prices → evaluate signals → risk-gate → place orders via IBKR → monitor. Starts on **IBKR paper trading**.
- **Is NOT:** the existing `stocks` dashboard (a separate project — a batch research/analysis dashboard). nomad is **fully isolated** from it: own repo, own k8s namespace (`nomad`), own DB, own secrets, own deploy, own ingress host. nomad may *copy* code from `stocks` but shares no runtime.

## Why separate from `stocks`
`stocks` is batch + read-only (a collector refreshes Postgres every ~30 min; the UI only reads). An auto-trader is the opposite: low-latency, event-driven, stateful, executes real money. Bolting one onto the other would compromise both. See `docs/DECISIONS.md`.

## Docs
- [`CLAUDE.md`](CLAUDE.md) — project memory for AI sessions (decisions, conventions, deploy pattern).
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — components and how they fit.
- [`docs/DECISIONS.md`](docs/DECISIONS.md) — broker, data, isolation, risk decisions + rationale.
- [`docs/REUSE.md`](docs/REUSE.md) — exact files to copy/port from the `stocks` repo.
- [`docs/ROADMAP.md`](docs/ROADMAP.md) — phased plan (gateway connect → paper MVP → backtest → live).

## Open decisions (resolve before P1 coding)
1. First-milestone scope: connect+monitor-only vs. full auto-paper MVP vs. semi-auto (human approves).
2. Starting strategy: port the `stocks` swing rules as a baseline vs. a new intraday strategy vs. one simple validation rule.
