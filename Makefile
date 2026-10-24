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

discover:          # refresh the pools table (allowlisted tokens x factories); add ARGS=--all for everything
	$(PY) -m mev.pools.discovery $(ARGS)

# Laptop agent: alerts, on-chain verification and lifetime tracking for stored opportunities
agent:
	$(PY) -m mev.agent

# Kubernetes (kubectl context: prodaiq-oke-test, namespace mev-platform)
K8S_NS=mev-platform
k8s-apply:
	kubectl apply -k infrastructure/kubernetes
k8s-secret:        # alert webhook / LLM key / RPC override from .env (never committed)
	kubectl -n $(K8S_NS) create secret generic mev-env --from-env-file=.env --dry-run=client -o yaml | kubectl apply -f -
k8s-status:
	kubectl -n $(K8S_NS) get cluster,deploy,pod,svc
k8s-logs:
	kubectl -n $(K8S_NS) logs -f deploy/mev-loop --tail=50
k8s-api:           # then open http://localhost:8000/pnl
	kubectl -n $(K8S_NS) port-forward svc/mev-api 8000:8000
