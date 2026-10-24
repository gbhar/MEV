#!/usr/bin/env bash
# One-shot setup for a fresh Ubuntu 22.04/24.04 VM (tested layout: Oracle Cloud Always Free, Ampere A1).
# Run as the default user (ubuntu) from inside the repository directory:
#   bash deploy/setup.sh
# It installs Docker, creates .env from the example if missing, and starts loop + agent + api.
set -euo pipefail
cd "$(dirname "$0")/.."

if ! command -v docker >/dev/null 2>&1; then
  echo "== installing docker"
  curl -fsSL https://get.docker.com | sudo sh
  sudo usermod -aG docker "$USER"
  echo "== docker installed; this shell lacks the docker group until you log out and back in, using sudo for now"
  DOCKER="sudo docker"
else
  DOCKER="docker"
fi

if [ ! -f .env ]; then
  cp .env.example .env
  echo "== created .env from .env.example; set MEV_ALERT_WEBHOOK (Telegram) and optionally MEV_LLM_API_KEY, then rerun"
fi
mkdir -p data

echo "== starting the stack"
$DOCKER compose -f docker-compose.app.yml up -d --build
$DOCKER compose -f docker-compose.app.yml ps
echo
echo "logs:      $DOCKER compose -f docker-compose.app.yml logs -f loop agent"
echo "API:       ssh -L 8000:127.0.0.1:8000 <user>@<vm-ip>  then open http://localhost:8000/pnl"
echo "stop:      $DOCKER compose -f docker-compose.app.yml down"
