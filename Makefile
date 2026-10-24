PY=.venv/bin/python

install:
	python3 -m venv .venv && .venv/bin/pip install -q --upgrade pip && .venv/bin/pip install -q -e ".[dev]"

test:
	$(PY) -m pytest -q

check-rpc:
	$(PY) scripts/check_rpc.py

run:
	$(PY) -m mev.main

# Process N blocks then exit (useful for smoke tests)
run-once:
	$(PY) -m mev.main --blocks 3

api:
	$(PY) -m uvicorn mev.api.app:app --host 127.0.0.1 --port 8000

# Reproduce a block range into a separate database (never touches mev.db)
replay:
	$(PY) -m mev.replay --from-block $(FROM) --to-block $(TO) --database-url sqlite:///replay.db

export:
	$(PY) scripts/export_dataset.py --out dataset.csv

monitoring:
	docker compose up -d prometheus grafana

live-tests:
	MEV_LIVE_TESTS=1 $(PY) -m pytest -q

# Laptop agent: alerts, on-chain verification and lifetime tracking for stored opportunities
agent:
	$(PY) -m mev.agent
