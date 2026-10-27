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

retain:            # drop raw state older than MEV_RETENTION_HOURS (24) and losing decisions older than MEV_RETENTION_DECISION_HOURS (168); ARGS=--vacuum shrinks the SQLite file
	$(PY) -m mev.db.retention $(ARGS)

# Laptop agent: alerts, on-chain verification and lifetime tracking for stored opportunities
agent:
	$(PY) -m mev.agent

# Execution (spec Phases 5-7): contract, wallet, deployment. Live sends need all three plus MEV_EXECUTION_MODE=live.
contract-build:    # needs Foundry (brew install foundry)
	cd contracts && forge build
contract-test:     # fork tests against Base at the paper-trade blocks; MEV_RPC_HTTP must be an archive endpoint
	cd contracts && set -a && . ../.env && set +a && forge test -vv
contract-abi:      # regenerate mev/chain/abi.py's FLASH_ARB after changing the contract
	$(PY) scripts/gen_abi.py
wallet:            # new signer key -> Kubernetes secret mev-signer; prints the address to fund
	$(PY) scripts/wallet.py create
wallet-address:
	$(PY) scripts/wallet.py address
wallet-balance:
	$(PY) scripts/wallet.py balance
contract-deploy:   # deploy FlashArb with the signer; then put the printed address in .env as MEV_FLASH_ARB_ADDRESS
	$(PY) scripts/deploy_contract.py
halt:              # kill switch: stop live sends (clear with `make resume`)
	$(PY) -m mev.execution.killswitch on manual
resume:
	$(PY) -m mev.execution.killswitch off

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
