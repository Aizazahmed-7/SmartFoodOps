COMPOSE := docker compose -f deploy/compose/docker-compose.yml

.PHONY: up up-apps up-ui up-lean up-m2 up-m3 up-full down nuke logs psql test cov dev seed demo chaos chaos-off fmt lint migrate

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

up-lean: ## W1 working set (~4 GB) — skips temporal/localstack/mock-psp until W2 needs them
	$(COMPOSE) --profile core --profile apps up -d --wait postgres redis kafka schema-registry gateway identity catalog edge-bff
	@echo "✔ lean stack up — gateway http://localhost:8080 · frontend proxies here"

up-m2: ## Inventory+Orders milestone working set (~6-7 GB): W1 set + temporal, mock-psp, inventory, order(+worker), payment
	$(COMPOSE) --profile core --profile apps up -d --wait postgres redis kafka schema-registry temporal mock-psp gateway identity catalog edge-bff inventory order order-worker payment
	@echo "✔ m2 stack up — gateway :8080 · temporal-ui :8233 · mock-psp :9080"

up-m3: ## Notifications milestone working set: the m2 set + notification
	$(COMPOSE) --profile core --profile apps up -d --wait postgres redis kafka schema-registry temporal mock-psp gateway identity catalog edge-bff inventory order order-worker payment notification analytics
	@# initdb scripts only run on FRESH volumes; converge existing ones so a
	@# newly added service's database appears without `make nuke`. The script
	@# is idempotent; a crash-looping service recovers on its next restart.
	@$(COMPOSE) exec -T postgres bash /docker-entrypoint-initdb.d/01-databases.sh >/dev/null
	@echo "✔ m3 stack up — gateway :8080 · temporal-ui :8233 · notifications :8008 · analytics :8009"

dlq-replay: ## Replay parked DLQ events after a fix: make dlq-replay TOPIC=c1.orders.events.dlq
	uv run --package smartfood-kafka python -m smartfood_kafka.replay $(TOPIC)

up-obs: ## m3 set + Prometheus (:9090), Grafana (:3000), Jaeger (:16686), Alertmanager (:9093)
	OTLP_ENDPOINT=http://jaeger:4318 $(COMPOSE) --profile core --profile apps --profile obs up -d --wait postgres redis kafka schema-registry temporal mock-psp gateway identity catalog edge-bff inventory order order-worker payment notification analytics prometheus grafana jaeger alertmanager cadvisor kafka-exporter loki promtail canary
	@# Same initdb convergence as up-m3: a newly added service's database
	@# must appear without `make nuke`, whichever target brought the stack up.
	@$(COMPOSE) exec -T postgres bash /docker-entrypoint-initdb.d/01-databases.sh >/dev/null
	@echo "✔ obs stack up — grafana :3000 · prometheus :9090 · jaeger :16686 · alertmanager :9093"

up-full: up up-apps up-ui

down:
	$(COMPOSE) --profile core --profile apps --profile ui --profile obs down

nuke: ## Down + delete all volumes (fresh start)
	$(COMPOSE) --profile core --profile apps --profile ui --profile obs down -v

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
		--cov=smartfood_outbox --cov=smartfood_pricing --cov=smartfood_idempotency \
		--cov=identity --cov=edge_bff \
		--cov=catalog --cov=inventory --cov=order --cov=payment --cov=notification --cov=analytics \
		--cov=mock_psp --cov=seed --cov=canary \
		--cov-fail-under=100 \
		--cov-report=term-missing

seed:
	uv run --package seed python -m seed.main

demo:
	./tools/demo/place-order.sh

chaos: ## Raise mock-psp failure knobs and restart it
	TIMEOUT_RATE=0.5 DECLINE_RATE=0.2 $(COMPOSE) --profile core up -d mock-psp
	$(COMPOSE) exec mock-psp env | grep -E "DECLINE_RATE|TIMEOUT_RATE"
	@echo "✔ mock-psp now timing out 50% and declining 20% — place orders and watch compensations in Temporal UI"

chaos-off: ## Restore mock-psp failure knobs to their defaults
	$(COMPOSE) --profile core up -d mock-psp
	$(COMPOSE) exec mock-psp env | grep -E "DECLINE_RATE|TIMEOUT_RATE"
	@echo "✔ mock-psp back to defaults (DECLINE_RATE=0.0, TIMEOUT_RATE=0.0)"

fmt:
	uv run ruff check --fix . && uv run ruff format .

lint: ## Static checks — ruff + pyright, both gating (strict tier per pyproject [tool.pyright])
	uv run ruff check . && uv run ruff format --check .
	uv run --no-sync pyright

migrate: ## Apply one service's Alembic migrations: make migrate SVC=identity [DATABASE_URL=...]
	uv run --package $(SVC) python -c "import os; from alembic import command; from alembic.config import Config; \
	from $(subst -,_,$(SVC)).config import Settings; \
	cfg = Config(); cfg.set_main_option('script_location', 'services/$(SVC)/migrations'); \
	cfg.set_main_option('sqlalchemy.url', os.environ.get('DATABASE_URL') or Settings().database_url); \
	command.upgrade(cfg, 'head')"

up-cdc: ## Debezium CDC lane (S6): logical decoding on postgres + Kafka Connect (:8083)
	$(COMPOSE) --profile core --profile cdc up -d --wait postgres kafka
	$(COMPOSE) --profile core --profile cdc up -d kafka-connect
	@echo "✔ connect up — register with make cdc-register, watch cdc.c1.orders.events"

cdc-register: ## Register (or update) the order-outbox Debezium connector
	@curl -s -X PUT http://localhost:8083/connectors/order-outbox/config \
	  -H 'Content-Type: application/json' \
	  -d @deploy/compose/cdc/order-outbox.json.config >/dev/null \
	  || (echo "connect not up yet — make up-cdc first" && exit 1)
	@curl -s http://localhost:8083/connectors/order-outbox/status | python3 -m json.tool
