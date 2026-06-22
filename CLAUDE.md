# nomad — project memory (read this first)

nomad is a **real-time intraday auto-trading** app for US equities, executing via **Interactive Brokers**. It is a **separate project** from the `stocks` analysis dashboard (different repo, k8s namespace, DB, secrets, deploy, ingress). It may copy code from `stocks` but shares no runtime.

Owner: Eitan (`eitanherman@gmail.com`). Swing/active trader. Existing `stocks` repo lives at `/Users/master/Documents/projects/stocks` (same machine) — the source to copy/port from.

## Settled decisions
- **Broker = Interactive Brokers (IBKR)** — the user already has funds there (NOT Alpaca). Use **`ib_insync`** (Python) talking to a **headless IB Gateway** running in-cluster.
- **Paper trading first.** The IBKR *paper* account has **no 2FA**, so the gateway runs unattended — use this to validate the whole stack before any real money. Live cutover (later) uses IB Key 2FA + IBC auto-restart.
- **Signal data = FMP real-time quote.** Verified real-time in the `stocks` app (FMP `/stable/quote` timestamp = now). massive.com is a Polygon whitelabel but **15-min delayed** on the Starter plan → research only, not for live trading signals.
- **Strict isolation from `stocks`** — own everything; a nomad crash/deploy must never affect `stocks`.
- **Money-safety is non-negotiable** from the first order-placing build: a single risk gate, position/exposure limits, per-trade stop-loss, max-daily-drawdown auto-halt, manual kill-switch, idempotent orders (client `orderRef`), reconciliation on reconnect, engine as a strict singleton.

## Architecture (see docs/ARCHITECTURE.md)
- `nomad-engine` — single asyncio process that owns ALL trading: feed → strategy → risk gate → order router → reconciler. Singleton (`replicas:1`, `Recreate`, fixed IBKR `clientId`).
- `nomad-api` — FastAPI read-only API + React monitoring UI (positions, PnL, orders, signals, gateway status, **kill-switch**). Never touches IBKR; reads/writes Postgres only.
- `ibgw` — separate Deployment running `ib-gateway-docker` (+ IBC) in paper mode; ClusterIP `ibgw:4002`, never public.
- **Postgres** — all state (orders, fills, positions, pnl, signals, config/kill-switch). It is the only cross-process boundary (engine writes, api reads).
- No queue / no microservices — single-user bot; in-process asyncio is simplest + safest.

## Deploy pattern (cloned from `stocks` — see docs/REUSE.md)
- k3s single node, SSH `root@skynet1`. **No registry**: rsync source → `docker build` on the node → `docker save | k3s ctr images import -` → `kubectl apply` → rollout restart. Images `:latest`, `imagePullPolicy: IfNotPresent`.
- Raw kubectl manifests in `deploy/*.yaml` (no Helm). Namespace `nomad`. Postgres uses `local-path` PVC, `Recreate`.
- Ingress: `ingressClassName: traefik`, `cert-manager.io/cluster-issuer: letsencrypt-prod`, entrypoints `web,websecure`, behind Cloudflare. Host: `nomad.securegion.com` (only the read-only UI is exposed; engine + ibgw are internal).
- Backend = Python 3.12 + FastAPI + httpx + asyncpg + pydantic-settings + **ib_insync**. Config via env/secrets (pydantic BaseSettings).
- Secrets (k8s + gitignored `.env`): `nomad-db` (Postgres), `fmp-api` (FMP_API_KEY), `ibkr-creds` (IB_USERNAME/IB_PASSWORD/TRADING_MODE=paper). Set via `kubectl create secret ... --dry-run=client -o yaml | kubectl apply -f -`.

## Conventions
- Never commit secrets; keep `.env` gitignored.
- Co-author commits with: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.
- User preference (from `stocks` work): after each fix, commit + open a PR + auto-merge. `gh` may need `gh auth login` first.

## Open decisions (resolve before P1 coding)
1. First-milestone scope — connect+monitor-only vs. full auto-paper MVP vs. semi-auto (human approves each).
2. Starting strategy — port `stocks` `analysis.ts` swing rules to `app/strategy.py` as a baseline (known-good, backtestable, but swing- not intraday-oriented) vs. design a new intraday strategy (momentum/VWAP/ATR) vs. one trivial rule to validate the pipeline.
