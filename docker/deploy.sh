#!/usr/bin/env bash
# deploy.sh — run this from your LOCAL machine
# Usage: bash docker/deploy.sh <droplet-ip> [ssh-user]
#
# What it does:
#   1. rsync the pre-processed event data to the server (one-time, ~500MB)
#   2. Push the latest code via git pull on the server
#   3. Rebuild and restart the Docker stack

set -euo pipefail

SERVER_IP="${1:?Usage: bash deploy.sh <droplet-ip> [ssh-user]}"
SSH_USER="${2:-root}"
SSH="ssh ${SSH_USER}@${SERVER_IP}"
REMOTE_DIR="/opt/wildfire"

echo "==> Deploying to ${SSH_USER}@${SERVER_IP}:${REMOTE_DIR}"

# ── 1. First-time: upload pre-processed event data ────────────────────────────
# Skip if data already exists on the server (safe to re-run)
echo "==> Syncing data/events/2016_0001 (skips files already present)..."
rsync -avz --progress \
  --exclude "timesteps/" \
  data/events/2016_0001/ \
  ${SSH_USER}@${SERVER_IP}:${REMOTE_DIR}/data/events/2016_0001/

# ── 2. Ensure repo is present on server ───────────────────────────────────────
echo "==> Updating code on server..."
$SSH "
  set -e
  if [ ! -d '${REMOTE_DIR}/.git' ]; then
    git clone https://github.com/geo-raypan/wildfire-decision-support.git ${REMOTE_DIR}
  else
    cd ${REMOTE_DIR} && git pull --ff-only
  fi
"

# ── 3. Upload .env (never committed to git) ───────────────────────────────────
echo "==> Uploading .env..."
scp .env ${SSH_USER}@${SERVER_IP}:${REMOTE_DIR}/.env

# ── 4. Rebuild and restart ────────────────────────────────────────────────────
echo "==> Building and starting containers..."
$SSH "
  cd ${REMOTE_DIR}/docker
  docker compose -f docker-compose.prod.yml pull caddy db
  docker compose -f docker-compose.prod.yml up -d --build
"

echo ""
echo "✓ Done! App should be live at https://YOUR_DOMAIN in ~30 seconds."
echo "  Check logs: ssh ${SSH_USER}@${SERVER_IP} 'cd ${REMOTE_DIR}/docker && docker compose -f docker-compose.prod.yml logs -f backend'"
