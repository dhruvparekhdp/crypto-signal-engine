# One-command dev setup. Tests do not use the network or exchange keys.
# Lint and typecheck fail only on findings that are new versus quality/*-baseline.txt.

VENV := .venv
PY := $(VENV)/bin/python
PIP := $(VENV)/bin/pip

.PHONY: setup lint typecheck test audit secrets check

setup:
	python3 -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt -r requirements-dev.txt

lint:
	$(PY) scripts/dev_gate.py lint

typecheck:
	$(PY) scripts/dev_gate.py typecheck

test:
	rm -f crypto_engine.db
	CRYPTO_ENGINE_ENV_FILE= $(PY) -m pytest -q --tb=short

audit:
	$(PY) scripts/dev_gate.py audit

secrets:
	gitleaks detect --redact --no-banner

check: lint typecheck test
