#!/usr/bin/env bash
# Deploy nomad to k3s (skynet1), namespace `nomad`. No external registry:
# public images are imported into k3s containerd via `docker save | ctr import`;
# our own images (Phase 1+) are built on the node the same way. Run from repo root.
#
#   ./scripts/deploy.sh            # sources ./.env for creds
#
# Phase 0 scope: namespace + secrets + Postgres + ibgw (gateway connectivity).
# Phase 1 will add the nomad-engine / nomad-api images + ingress (guarded below).
set -euo pipefail

NODE="${NODE:-root@skynet1}"
REMOTE_DIR="${REMOTE_DIR:-/root/nomad}"
NS=nomad

[ -f .env ] && set -a && . ./.env && set +a
: "${IB_USERNAME:?IB_USERNAME must be set (env or .env) — your PAPER login}"
: "${IB_PASSWORD:?IB_PASSWORD must be set (env or .env)}"
: "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD must be set (env or .env)}"
TRADING_MODE="${TRADING_MODE:-paper}"
DATABASE_URL="postgresql://nomad:${POSTGRES_PASSWORD}@postgres:5432/nomad"

if [ "$TRADING_MODE" != "paper" ]; then
  echo "‼ TRADING_MODE=$TRADING_MODE — refusing to deploy non-paper in Phase 0." >&2
  exit 1
fi

echo "==> Syncing source to ${NODE}:${REMOTE_DIR}"
rsync -az --delete \
  --exclude '.git' --exclude '.env' --exclude 'node_modules' \
  --exclude '.venv' --exclude '.cache' --exclude 'dist' \
  ./ "${NODE}:${REMOTE_DIR}/"

echo "==> Building the derived IB Gateway image (base + dialog-dismisser) on the node"
ssh "$NODE" "set -e
  cd ${REMOTE_DIR}
  docker build -t nomad-ibgw:latest ./deploy/ibgw
  docker save nomad-ibgw:latest | k3s ctr images import -
"

echo "==> Applying namespace + secrets"
ssh "$NODE" "set -e
  kubectl apply -f ${REMOTE_DIR}/deploy/namespace.yaml
  kubectl -n ${NS} create secret generic ibkr-creds \
    --from-literal=IB_USERNAME='${IB_USERNAME}' \
    --from-literal=IB_PASSWORD='${IB_PASSWORD}' \
    --from-literal=TRADING_MODE='${TRADING_MODE}' \
    --dry-run=client -o yaml | kubectl apply -f -
  kubectl -n ${NS} create secret generic nomad-db \
    --from-literal=POSTGRES_USER='nomad' \
    --from-literal=POSTGRES_PASSWORD='${POSTGRES_PASSWORD}' \
    --from-literal=POSTGRES_DB='nomad' \
    --from-literal=DATABASE_URL='${DATABASE_URL}' \
    --dry-run=client -o yaml | kubectl apply -f -
"

echo "==> Deploying Postgres + ibgw"
ssh "$NODE" "set -e
  kubectl apply -f ${REMOTE_DIR}/deploy/postgres.yaml -f ${REMOTE_DIR}/deploy/ibgw.yaml
  kubectl -n ${NS} rollout status deploy/postgres --timeout=180s
  kubectl -n ${NS} rollout status deploy/ibgw --timeout=180s
"

# --- Phase 1+ (engine/api images + ingress) — enabled once their Dockerfiles exist ---
# ssh "$NODE" "set -e
#   cd ${REMOTE_DIR}
#   docker build -t nomad-engine:latest ./backend
#   docker build -t nomad-api:latest ./frontend
#   docker save nomad-engine:latest | k3s ctr images import -
#   docker save nomad-api:latest    | k3s ctr images import -
#   kubectl apply -f ${REMOTE_DIR}/deploy/engine.yaml -f ${REMOTE_DIR}/deploy/api.yaml -f ${REMOTE_DIR}/deploy/ingress.yaml
#   kubectl -n ${NS} rollout restart deploy/nomad-engine deploy/nomad-api
# "

echo "==> Done. Verify the gateway, then run the Phase 0 smoke test locally:"
echo "    kubectl -n ${NS} port-forward svc/ibgw 4002:4002 &"
echo "    (cd backend && . .venv/bin/activate && python ../scripts/check_ibkr.py)"
