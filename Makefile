# Nurse Worry Layer — API
#
# `make` with no target prints this help.

SHELL := /bin/bash
.DEFAULT_GOAL := help
# Keep stdout clean so targets can be piped (make me | jq).
MAKEFLAGS += --no-print-directory

VENV    := .venv
PY      := $(VENV)/bin/python
PIP     := $(VENV)/bin/pip
PORT    := 5001
LOGDIR  := /tmp/nursing-logs
PIDFILE := $(LOGDIR)/api.pid

.PHONY: help install dev start stop restart status health test test-rls test-all \
        migrate migrate-print seed token me logs clean

help: ## Show this help
	@echo "Nurse Worry Layer — API (port $(PORT))"
	@echo
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "} {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'
	@echo

install: ## Create the venv and install dependencies
	@test -d $(VENV) || python3 -m venv $(VENV)
	@$(PIP) install -q --upgrade pip
	@$(PIP) install -q -r requirements.txt
	@echo "Dependencies installed."
	@test -f .env || { cp .env.example .env; echo "Created .env — fill in SUPABASE_SERVICE_KEY."; }

dev: ## Run the API in the foreground (Ctrl-C to stop)
	@$(PY) run.py

start: ## Run the API in the background
	@mkdir -p $(LOGDIR)
	@if [ -f $(PIDFILE) ] && kill -0 $$(cat $(PIDFILE)) 2>/dev/null; then \
		echo "Already running (pid $$(cat $(PIDFILE)))."; \
	else \
		rm -f /tmp/nurse_worry_scheduler.lock; \
		nohup $(PY) run.py > $(LOGDIR)/api.log 2>&1 & echo $$! > $(PIDFILE); \
		sleep 5; \
		$(MAKE) --no-print-directory health; \
	fi

stop: ## Stop the background API and free the port
	@pids=$$(ss -lptn 'sport = :$(PORT)' 2>/dev/null | grep -oP 'pid=\K[0-9]+' | sort -u); \
	if [ -n "$$pids" ]; then kill $$pids 2>/dev/null; sleep 2; \
		pids=$$(ss -lptn 'sport = :$(PORT)' 2>/dev/null | grep -oP 'pid=\K[0-9]+' | sort -u); \
		[ -n "$$pids" ] && kill -9 $$pids 2>/dev/null; \
		echo "Stopped."; \
	else echo "Not running."; fi
	@rm -f $(PIDFILE) /tmp/nurse_worry_scheduler.lock

restart: stop start ## Restart the background API

status: ## Show whether the API is listening
	@pid=$$(ss -lptn 'sport = :$(PORT)' 2>/dev/null | grep -oP 'pid=\K[0-9]+' | head -1); \
	if [ -n "$$pid" ]; then echo "API running on :$(PORT) (pid $$pid)"; \
	else echo "API not running"; fi

health: ## Hit /api/v1/health
	@curl -fsS localhost:$(PORT)/api/v1/health 2>/dev/null \
		| $(PY) -c "import json,sys; d=json.load(sys.stdin); print(f\"status={d['status']}  db={d['database']['reachable']}  jwt={d['jwt_scheme']}\")" \
		|| echo "API not responding on :$(PORT) — run 'make start'"

test: ## Run the unit tests (no network needed)
	@SUPABASE_URL=https://example.supabase.co SUPABASE_SERVICE_KEY=dummy $(PY) -m pytest tests/ -q

test-rls: ## Attack the RLS policies on a throwaway Postgres
	@./tests/sql/run.sh

test-all: test test-rls ## Run every test suite

migrate: ## Apply migrations (needs DATABASE_URL). The hosted DB is already migrated.
	@$(PY) scripts/apply_migrations.py

migrate-print: ## Print the combined SQL to paste into the Supabase SQL editor
	@$(PY) scripts/apply_migrations.py --print

seed: ## DESTRUCTIVE — delete and rebuild the demo org
	@echo "This DELETES the 'mercy-general' org and all its data, then rebuilds it."
	@read -p "Type 'yes' to continue: " ok; [ "$$ok" = "yes" ] || { echo "Aborted."; exit 1; }
	@$(PY) scripts/seed.py

ACCOUNT ?= charge

token: ## Print an access token. Usage: make token ACCOUNT=maria (default: charge)
	@set -a; . ./.env; set +a; \
	curl -s -X POST "$$SUPABASE_URL/auth/v1/token?grant_type=password" \
		-H "apikey: $$SUPABASE_ANON_KEY" -H "Content-Type: application/json" \
		-d "{\"email\":\"$(ACCOUNT)@mercygeneral.example\",\"password\":\"WorryLayer!2026\"}" \
		| $(PY) -c "import json,sys; d=json.load(sys.stdin); print(d.get('access_token') or d)"

me: ## Call /api/v1/me as ACCOUNT. Usage: make me ACCOUNT=maria
	@tok=$$($(MAKE) --no-print-directory token ACCOUNT=$(ACCOUNT)); \
	curl -s localhost:$(PORT)/api/v1/me -H "Authorization: Bearer $$tok" \
		| $(PY) -m json.tool

logs: ## Tail the background API log
	@tail -f $(LOGDIR)/api.log

clean: ## Remove caches and the venv
	@rm -rf $(VENV) .pytest_cache .ruff_cache
	@find . -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
	@echo "Cleaned."
