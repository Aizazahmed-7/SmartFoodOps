COMPOSE := docker compose -f deploy/compose/docker-compose.yml

.PHONY: up up-apps up-full down nuke logs psql test test-int dev seed demo chaos fmt lint migrate

up: ## Start core infrastructure (postgres, redis, kafka, temporal, localstack, mock-psp, gateway)
	$(COMPOSE) --profile core up -d --wait
	@echo "✔ core up — gateway http://localhost:8080 · temporal-ui http://localhost:8233"

up-apps: ## Start app services (all, or ONLY="identity order")
ifdef ONLY
	$(COMPOSE) --profile apps up -d $(ONLY)
else
	$(COMPOSE) --profile core --profile apps up -d --wait
endif

up-ui: ## Start management consoles (Kafka console :8085, Redis UI :8087); Postgres → desktop pgAdmin on localhost:5432
	$(COMPOSE) --profile core --profile ui up -d kafka-console redis-commander

up-lean: ## W1 working set (~4 GB) — skips temporal/localstack/rabbitmq/mock-psp until W2 needs them
	$(COMPOSE) --profile core --profile apps up -d --wait postgres redis kafka schema-registry gateway identity catalog edge-bff
	@echo "✔ lean stack up — gateway http://localhost:8080 · frontend proxies here"

up-full: up up-apps up-ui

down:
	$(COMPOSE) --profile core --profile apps --profile ui down

nuke: ## Down + delete all volumes (fresh start)
	$(COMPOSE) --profile core --profile apps --profile ui down -v

# Profile flags are required even for logs: compose can't resolve a
# profile-gated service (or its depends_on chain) without them.
logs: ## Tail logs (SVC=order for one service)
ifdef SVC
	$(COMPOSE) --profile core --profile apps --profile ui logs -f $(SVC)
else
	$(COMPOSE) --profile core --profile apps --profile ui logs -f
endif

psql: ## Shell into a service DB: make psql DB=order_db
	$(COMPOSE) exec postgres psql -U sfo -d $(or $(DB),sfo)

dev: ## Run one service natively with reload: make dev SVC=order
	uv run --package $(SVC) uvicorn $(subst -,_,$(SVC)).main:app --reload --port $(PORT)

test: ## Unit tests (no infra needed)
	uv sync --all-packages -q && uv run --no-sync pytest -q

# Explicit package list: skeleton services (healthz stubs) would only add noise.
# Add each service's package here when its first real code lands.
cov: ## Unit tests + coverage report
	uv sync --all-packages -q && uv run --no-sync pytest -q \
		--cov=smartfood_api --cov=smartfood_auth --cov=smartfood_kafka --cov=smartfood_otel \
		--cov=smartfood_outbox --cov=identity --cov=edge_bff --cov=catalog \
		--cov=seed \
		--cov-report=term-missing

seed:
	uv run --package seed python -m seed.main

demo:
	./tools/demo/place-order.sh

chaos: ## Raise mock-psp failure knobs and restart it
	TIMEOUT_RATE=0.5 DECLINE_RATE=0.2 $(COMPOSE) --profile core up -d mock-psp
	@echo "✔ mock-psp now timing out 50% and declining 20% — place orders and watch compensations in Temporal UI"

fmt:
	uv run ruff check --fix . && uv run ruff format .

lint: ## Static checks — ruff gates now; pyright reports (enforcement activates with Catalog)
	uv run ruff check . && uv run ruff format --check .
	-@uv run --no-sync pyright || echo "pyright: informational until the Catalog milestone (repo-structure.md §5)"

migrate: ## Apply one service's Alembic migrations: make migrate SVC=identity [DATABASE_URL=...]
	uv run --package $(SVC) python -c "import os; from alembic import command; from alembic.config import Config; \
	from $(subst -,_,$(SVC)).config import Settings; \
	cfg = Config(); cfg.set_main_option('script_location', 'services/$(SVC)/migrations'); \
	cfg.set_main_option('sqlalchemy.url', os.environ.get('DATABASE_URL') or Settings().database_url); \
	command.upgrade(cfg, 'head')"
