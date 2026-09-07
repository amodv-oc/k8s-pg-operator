# pg-operator development tasks.
#
# Integration tests need a live PostgreSQL server reachable via PGOP_TEST_DSN.
# `make test-setup` starts one configured like RDS: the managing role is a
# member of rds_superuser with CREATEDB and CREATEROLE, but is not a superuser.

.DEFAULT_GOAL := help
SHELL := /bin/bash

IMAGE       ?= pg-operator
TAG         ?= dev
CHART       := charts/pg-operator
NAMESPACE   ?= pg-operator
PG_CONTAINER?= pgop-test
PG_PORT     ?= 15432
PG_IMAGE    ?= postgres:16-alpine
TEST_DSN    := host=localhost port=$(PG_PORT) user=pgadmin password=adminpw dbname=postgres sslmode=disable

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

.PHONY: install
install: ## Sync the virtualenv, including dev dependencies
	uv sync --extra dev

.PHONY: lint
lint: ## Ruff lint
	uv run ruff check src tests

.PHONY: format
format: ## Ruff autofix and format
	uv run ruff check --fix src tests
	uv run ruff format src tests

.PHONY: typecheck
typecheck: ## mypy
	uv run mypy src/pg_operator

.PHONY: test
test: ## Unit tests (integration tests skip without PGOP_TEST_DSN)
	uv run pytest

.PHONY: test-setup
test-setup: ## Start a PostgreSQL container with an RDS-like managing role
	-docker rm -f $(PG_CONTAINER)
	docker run -d --name $(PG_CONTAINER) \
	  -e POSTGRES_PASSWORD=testpw -p $(PG_PORT):5432 $(PG_IMAGE)
	@for i in $$(seq 1 30); do \
	  docker exec $(PG_CONTAINER) pg_isready -U postgres >/dev/null 2>&1 && break; \
	  sleep 1; \
	done
	docker exec $(PG_CONTAINER) psql -U postgres -c "CREATE ROLE rds_superuser NOLOGIN"
	docker exec $(PG_CONTAINER) psql -U postgres -c \
	  "CREATE ROLE pgadmin LOGIN PASSWORD 'adminpw' CREATEDB CREATEROLE"
	docker exec $(PG_CONTAINER) psql -U postgres -c "GRANT rds_superuser TO pgadmin"
	@echo
	@echo 'export PGOP_TEST_DSN="$(TEST_DSN)"'

.PHONY: test-integration
test-integration: ## Full suite against the container from test-setup
	PGOP_TEST_DSN="$(TEST_DSN)" uv run pytest

.PHONY: test-teardown
test-teardown: ## Remove the PostgreSQL container
	-docker rm -f $(PG_CONTAINER)

.PHONY: coverage
coverage: ## Full suite with a coverage report
	PGOP_TEST_DSN="$(TEST_DSN)" uv run pytest \
	  --cov=pg_operator --cov-report=term-missing --cov-report=html

.PHONY: check
check: lint typecheck test chart-lint ## Everything CI runs

.PHONY: chart-lint
chart-lint: ## helm lint and a render of every feature
	helm lint $(CHART)
	helm template test $(CHART) --namespace $(NAMESPACE) >/dev/null
	helm template test $(CHART) --namespace $(NAMESPACE) \
	  --set replicaCount=2 --set peering.enabled=true \
	  --set metrics.serviceMonitor.enabled=true \
	  --set podDisruptionBudget.enabled=true \
	  --set networkPolicy.enabled=true \
	  --set api.ingress.enabled=true \
	  --set rbac.namespacedSecrets=true \
	  --set 'rbac.secretNamespaces={team-a}' >/dev/null
	@echo "chart renders cleanly"

.PHONY: chart-render
chart-render: ## Print the rendered chart
	helm template $(NAMESPACE) $(CHART) --namespace $(NAMESPACE)

.PHONY: build
build: ## Build the container image
	docker build -t $(IMAGE):$(TAG) .

.PHONY: image-check
image-check: build ## Confirm both entrypoints resolve in the image
	docker run --rm --user 65532:65532 $(IMAGE):$(TAG) \
	  python -c "import pg_operator.operator, pg_operator.api.main; print('ok')"
	docker run --rm --user 65532:65532 $(IMAGE):$(TAG) kopf --version

.PHONY: crds
crds: ## Apply the CRDs (Helm does not upgrade CRDs in place)
	kubectl apply -f $(CHART)/crds/

.PHONY: deploy
deploy: ## Install or upgrade the chart
	helm upgrade --install $(NAMESPACE) $(CHART) \
	  --namespace $(NAMESPACE) --create-namespace --wait

.PHONY: run
run: ## Run the operator against the current kubecontext
	PG_OPERATOR_LOG_FORMAT=console PG_OPERATOR_NAMESPACE=$(NAMESPACE) \
	  uv run kopf run --module pg_operator.operator --all-namespaces --standalone

.PHONY: run-api
run-api: ## Run the state API against the current kubecontext
	PG_OPERATOR_LOG_FORMAT=console uv run pg-operator-api

.PHONY: clean
clean: ## Remove build and cache artefacts
	rm -rf .pytest_cache .ruff_cache .mypy_cache htmlcov .coverage dist build
	find . -type d -name __pycache__ -not -path "./.venv/*" -prune -exec rm -rf {} +
