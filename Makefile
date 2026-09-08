# pg-operator development tasks.
#
# Integration tests need a live PostgreSQL server reachable via PGOP_TEST_DSN.
# `make test-setup` starts one configured like RDS: the managing role is a
# member of rds_superuser with CREATEDB and CREATEROLE, but is not a superuser.

.DEFAULT_GOAL := help
SHELL := /bin/bash

IMAGE       ?= pg-operator
UI_IMAGE    ?= pg-operator-ui
TAG         ?= dev
UI_DIR      := ui
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
check: lint typecheck test chart-lint ui-check ## Everything CI runs

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
	helm template test $(CHART) --namespace $(NAMESPACE) \
	  --set ui.ingress.enabled=true \
	  --set networkPolicy.enabled=true >/dev/null
	helm template test $(CHART) --namespace $(NAMESPACE) \
	  --set ui.enabled=false --set api.enabled=false >/dev/null
	@! helm template test $(CHART) --namespace $(NAMESPACE) \
	  --set api.enabled=false >/dev/null 2>&1 \
	  || (echo "ui.enabled without api.enabled should have failed"; exit 1)
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

# --------------------------------------------------------------------------
# dashboard (ui/) - plain-JavaScript React on Mantine, served by nginx
# --------------------------------------------------------------------------

.PHONY: ui-install
ui-install: ## Install the dashboard's npm dependencies from the lockfile
	cd $(UI_DIR) && npm ci

.PHONY: ui-dev
ui-dev: ## Vite dev server, proxying the API on localhost:8000 (see local-api)
	cd $(UI_DIR) && npm run dev

.PHONY: ui-lint
ui-lint: ## ESLint the dashboard
	cd $(UI_DIR) && npm run lint

.PHONY: ui-build
ui-build: ## Build the dashboard bundle
	cd $(UI_DIR) && npm run build

.PHONY: ui-check
ui-check: ui-lint ui-build ## Everything CI runs for the dashboard

.PHONY: ui-image
ui-image: ## Build the dashboard container image
	docker build -t $(UI_IMAGE):$(TAG) $(UI_DIR)

.PHONY: ui-image-check
ui-image-check: ui-image ## Confirm the image serves under the pod's constraints
	@docker rm -f pgop-ui-check >/dev/null 2>&1 || true
	docker run -d --name pgop-ui-check --read-only --user 65532:65532 \
	  --tmpfs /tmp -p 8099:8081 $(UI_IMAGE):$(TAG) >/dev/null
	@for i in $$(seq 1 20); do \
	  curl -sf http://localhost:8099/nginx-healthz >/dev/null 2>&1 && break; \
	  sleep 0.5; \
	done
	@curl -sf http://localhost:8099/nginx-healthz >/dev/null \
	  || (echo "nginx did not come up read-only as uid 65532"; \
	      docker logs pgop-ui-check; docker rm -f pgop-ui-check; exit 1)
	@curl -s http://localhost:8099/ | grep -q '<div id="root"></div>' \
	  || (echo "index.html was not served"; docker rm -f pgop-ui-check; exit 1)
	@test "$$(curl -s -o /dev/null -w '%{http_code}' \
	  http://localhost:8099/databases/team-a/orders)" = "200" \
	  || (echo "the SPA fallback is not serving deep links"; \
	      docker rm -f pgop-ui-check; exit 1)
	@test "$$(curl -s -o /dev/null -w '%{http_code}' \
	  http://localhost:8099/api/v1/overview)" = "502" \
	  || (echo "/api is not proxied to 127.0.0.1:8000"; \
	      docker rm -f pgop-ui-check; exit 1)
	@docker rm -f pgop-ui-check >/dev/null
	@echo "dashboard image serves read-only as uid 65532"

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

# --------------------------------------------------------------------------
# local cluster (OrbStack or any local kubecontext) - see hack/local/README.md
# --------------------------------------------------------------------------

LOCAL_DIR   := hack/local
LOCAL_PGS   := 14 15 16 17 18

.PHONY: local-up
local-up: build ui-image ## Build, start PostgreSQL 14-18 in-cluster, install the operator
	kubectl apply -f $(LOCAL_DIR)/00-servers.yaml
	@for v in $(LOCAL_PGS); do \
	  kubectl rollout status -n pg-instances deploy/pg$$v --timeout=180s; \
	done
	kubectl apply -f $(CHART)/crds/
	kubectl apply -f $(LOCAL_DIR)/01-instances.yaml
	helm upgrade --install $(NAMESPACE) $(CHART) --namespace $(NAMESPACE) \
	  --set image.repository=$(IMAGE) --set image.tag=$(TAG) \
	  --set image.pullPolicy=IfNotPresent \
	  --set ui.image.repository=$(UI_IMAGE) --set ui.image.tag=$(TAG) \
	  --set ui.image.pullPolicy=IfNotPresent \
	  --set ui.config.clusterLabel=orbstack \
	  --set ui.config.refreshIntervalMs=5000 \
	  --set logging.format=console \
	  --set reconcile.interval=60 --set reconcile.idle=5 \
	  --wait --timeout 3m
	kubectl apply -f $(LOCAL_DIR)/02-workload.yaml
	@echo
	@echo "applied; run 'make local-status' once the timers have fired"

.PHONY: local-reload
local-reload: build ui-image ## Rebuild both images and restart the pod
	kubectl apply -f $(CHART)/crds/
	kubectl rollout restart -n $(NAMESPACE) deploy/$(NAMESPACE)
	kubectl rollout status -n $(NAMESPACE) deploy/$(NAMESPACE) --timeout=180s

.PHONY: local-status
local-status: ## Phases for every managed resource
	@kubectl get postgresinstances
	@echo
	@kubectl get postgresdbs -n team-a
	@echo
	@kubectl get postgresusers -n team-a

.PHONY: local-verify
local-verify: ## Connect as each generated user and assert its privileges
	@for v in $(LOCAL_PGS); do $(LOCAL_DIR)/verify.sh $$v || exit 1; done

.PHONY: local-test
local-test: ## Run the full pytest suite against each server in turn
	@for v in $(LOCAL_PGS); do \
	  kubectl port-forward -n pg-instances svc/pg$$v 15432:5432 >/dev/null 2>&1 & \
	  pf=$$!; \
	  until nc -z localhost 15432 2>/dev/null; do sleep 1; done; \
	  printf "PostgreSQL %-3s " $$v; \
	  PGOP_TEST_DSN="host=localhost port=15432 user=pgadmin password=adminpw dbname=postgres sslmode=disable" \
	    uv run pytest -q 2>&1 | tail -1; \
	  kill $$pf 2>/dev/null; wait $$pf 2>/dev/null; \
	done

.PHONY: local-logs
local-logs: ## Follow the operator log
	kubectl logs -n $(NAMESPACE) deploy/$(NAMESPACE) -c operator -f

.PHONY: local-api
local-api: ## Port-forward the state API to localhost:8000
	kubectl port-forward -n $(NAMESPACE) svc/$(NAMESPACE)-api 8000:8000

.PHONY: local-ui
local-ui: ## Port-forward the dashboard to localhost:8081
	@echo "dashboard: http://localhost:8081/"
	kubectl port-forward -n $(NAMESPACE) svc/$(NAMESPACE)-api 8081:8081

.PHONY: local-down
local-down: ## Remove the operator, the servers and their namespaces
	-kubectl delete -f $(LOCAL_DIR)/02-workload.yaml --ignore-not-found
	-kubectl delete -f $(LOCAL_DIR)/01-instances.yaml --ignore-not-found
	-kubectl delete -f $(LOCAL_DIR)/00-servers.yaml --ignore-not-found
	-helm uninstall $(NAMESPACE) --namespace $(NAMESPACE)

.PHONY: clean
clean: ## Remove build and cache artefacts
	rm -rf .pytest_cache .ruff_cache .mypy_cache htmlcov .coverage dist build
	find . -type d -name __pycache__ -not -path "./.venv/*" -prune -exec rm -rf {} +
