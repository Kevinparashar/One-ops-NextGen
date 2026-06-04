.PHONY: setup install up down test test-unit test-integration lint fmt typecheck clean proto proto-check ci ci-fast pmg-verify

PYTHON ?= python3.12
VENV ?= .venv
PIP = $(VENV)/bin/pip
PY = $(VENV)/bin/python
PYTEST = $(VENV)/bin/pytest
RUFF = $(VENV)/bin/ruff
MYPY = $(VENV)/bin/mypy

setup:
	$(PYTHON) -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -e ".[test,dev]"

install: setup

up:
	docker compose up -d
	@echo "Waiting for services..."
	@sleep 3
	@docker compose ps

down:
	docker compose down

test:
	$(PYTEST) -v

test-unit:
	$(PYTEST) -v -m unit tests/unit

test-integration:
	$(PYTEST) -v -m integration tests/integration

test-cov:
	$(PYTEST) --cov --cov-report=term-missing -v

lint:
	$(RUFF) check src tests

fmt:
	$(RUFF) format src tests
	$(RUFF) check --fix src tests

typecheck:
	$(MYPY) src/oneops

clean:
	rm -rf $(VENV) .pytest_cache .ruff_cache .mypy_cache htmlcov .coverage
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

# CI gate — local replacement for GitHub Actions. See docs/day1-execution-plan.md Phase 6.
# Stages: ruff → mypy → unit → integration → smoke → devil's-play. Fail-fast.
ci:
	bash scripts/ci.sh

# Fast CI gate — used by the pre-commit hook. Skips the integration suite.
ci-fast:
	bash scripts/ci.sh --fast

# PMG verifier — walks every Day-1 phase, checks evidence artefacts exist,
# writes ops/pmg-evidence/REPORT.md. See docs/day1-execution-plan.md "Master verification".
pmg-verify:
	bash ops/pmg-evidence/verify-all.sh

# Regenerate protobuf bindings from proto/ (ADR-0001).
proto:
	PYTHON=$(PY) bash tools/gen_proto.sh

# CI gate: regenerate and fail if the checked-in bindings are stale.
proto-check: proto
	@if ! git diff --quiet -- src/oneops/codec/generated; then \
		echo "ERROR: generated protobuf bindings are stale — run 'make proto' and commit"; \
		git diff --stat -- src/oneops/codec/generated; \
		exit 1; \
	fi
	@echo "protobuf bindings are up to date"
