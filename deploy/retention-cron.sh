#!/usr/bin/env bash
# Hourly retention for the docker-compose deployment: raw state 24 h, losing decisions 7 days (mev/db/retention.py).
# The 03:40 run also vacuums the SQLite file (a few seconds of exclusive lock; the loop retries blocks).
# Install:  ( crontab -l; echo "40 * * * * $HOME/mev/deploy/retention-cron.sh" ) | crontab -
cd "$(dirname "$0")/.." || exit 1
args=""
[ "$(date -u +%H)" = "03" ] && args="--vacuum"
sudo docker compose -f docker-compose.app.yml run --rm -T loop python -m mev.db.retention $args >> data/retention.log 2>&1
