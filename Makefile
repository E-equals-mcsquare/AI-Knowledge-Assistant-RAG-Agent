.PHONY: venv install setup run health

VENV     := .venv
PYTHON   := $(VENV)/bin/python
PIP      := $(VENV)/bin/pip
UVICORN  := $(VENV)/bin/uvicorn

HOST     := 0.0.0.0
PORT     := 8000

# ── Environment ──────────────────────────────────────────────────────────────

venv:
	python3 -m venv $(VENV)
	@echo "Virtualenv created. Run: source $(VENV)/bin/activate"

install:
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt

setup:
	@if [ ! -f .env ]; then \
		cp .env.example .env; \
		echo ".env created from .env.example — add your OPENAI_API_KEY"; \
	else \
		echo ".env already exists, skipping"; \
	fi

# ── Run ──────────────────────────────────────────────────────────────────────

run:
	$(UVICORN) app.main:app --reload --host $(HOST) --port $(PORT)

# ── Health check ─────────────────────────────────────────────────────────────

health:
	curl -s http://$(HOST):$(PORT)/health | python3 -m json.tool
