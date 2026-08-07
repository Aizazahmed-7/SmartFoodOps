COMPOSE := docker compose -f deploy/compose/docker-compose.yml

.PHONY: up up-apps up-full down nuke logs psql test test-int dev seed demo chaos fmt

up: ## Start core infrastructure (postgres, redis, kafka, temporal, localstack, mock-psp, gateway)
	$(COMPOSE) --profile core up -d --wait
	@echo "✔ core up — gateway http://localhost:8080 · temporal-ui http://localhost:8233"

up-apps: ## Start app services (all, or ONLY="identity order")
ifdef ONLY
	$(COMPOSE) --profile apps up -d $(ONLY)
else
	$(COMPOSE) --profile core --profile apps up -d --wait
endif

up-ui: ## Start management consoles (Kafka console :8085, Redis UI :8086); Postgres → desktop pgAdmin on localhost:5432
	$(COMPOSE) --profile core --profile ui up -d

up-full: up up-apps up-ui

down:
	$(COMPOSE) --profile core --profile apps down

nuke: ## Down + delete all volumes (fresh start)
	$(COMPOSE) --profile core --profile apps down -v

logs: ## Tail logs (SVC=order for one service)
ifdef SVC
	$(COMPOSE) logs -f $(SVC)
else
	$(COMPOSE) logs -f
endif

psql: ## Shell into a service DB: make psql DB=order_db
	$(COMPOSE) exec postgres psql -U sfo -d $(or $(DB),sfo)

dev: ## Run one service natively with reload: make dev SVC=order
	uv run --package $(SVC) uvicorn $(subst -,_,$(SVC)).main:app --reload --port $(PORT)

test: ## Unit tests (no infra needed)
	uv sync --all-packages -q && uv run --no-sync pytest -q

seed:
	uv run --package seed python -m seed.main

demo:
	./tools/demo/place-order.sh

chaos: ## Raise mock-psp failure knobs and restart it
	TIMEOUT_RATE=0.5 DECLINE_RATE=0.2 $(COMPOSE) --profile core up -d mock-psp
	@echo "✔ mock-psp now timing out 50% and declining 20% — place orders and watch compensations in Temporal UI"

fmt:
	uv run ruff check --fix . && uv run ruff format .
