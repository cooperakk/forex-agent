# Sentinel-FX -- the handful of commands that matter.
.DEFAULT_GOAL := help
PY := python3
VENV := .venv
BIN := $(VENV)/bin

.PHONY: help venv install dashboard test lint acceptance paper serve verify-audit docker clean package

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	 | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

venv:  ## Create the virtualenv
	$(PY) -m venv $(VENV)

install: venv  ## Install python + dashboard dependencies
	$(BIN)/pip install -U pip
	$(BIN)/pip install -r requirements-dev.txt
	cd dashboard && npm ci

dashboard:  ## Build the dashboard bundle into dashboard/dist
	cd dashboard && npm run build

test:  ## Run the full test suite
	$(BIN)/python -m pytest -q

lint:  ## Static checks
	$(BIN)/ruff check sentinel scripts tests

acceptance:  ## Run the acceptance protocol for a strategy (STRATEGY=donchian_trend)
	$(BIN)/python scripts/run_acceptance.py --strategy $(or $(STRATEGY),donchian_trend)

paper:  ## Run the paper simulation end to end
	$(BIN)/python scripts/run_paper_sim.py

serve:  ## Start the engine + dashboard on loopback
	$(BIN)/python scripts/serve.py

verify-audit:  ## Verify the audit hash chain
	$(BIN)/python -c "from sentinel.core.audit import AuditLog; \
	  ok,bad,msg = AuditLog('var/audit.jsonl').verify(); \
	  print('OK' if ok else f'BROKEN at seq {bad}: {msg}'); \
	  raise SystemExit(0 if ok else 1)"

docker:  ## Build the container image
	docker compose build

package:  ## Produce a distributable tarball
	./scripts/package.sh

clean:  ## Remove build artefacts (NOT var/ -- that is your state)
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
	rm -rf .pytest_cache .ruff_cache dashboard/dist dashboard/tsconfig.tsbuildinfo
