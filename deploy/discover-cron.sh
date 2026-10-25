#!/usr/bin/env bash
# Daily pool discovery for the docker-compose deployment (the cluster uses a CronJob for the same job).
# Install:  ( crontab -l; echo "17 3 * * * $HOME/mev/deploy/discover-cron.sh" ) | crontab -
cd "$(dirname "$0")/.." || exit 1
sudo docker compose -f docker-compose.app.yml run --rm -T loop python -m mev.pools.discovery >> data/discovery.log 2>&1
