# syntax=docker/dockerfile:1.7

# --------------------------------------------------------------------------
# builder: resolve and install dependencies with uv
# --------------------------------------------------------------------------
FROM python:3.14-slim AS builder

# Pinned to the uv version the lockfile was produced with.
COPY --from=ghcr.io/astral-sh/uv:0.12.10 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# Dependencies first, in their own layer: they change far less often than the
# source, so an edit to src/ does not re-resolve the whole environment.
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-install-project --no-dev

COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev

# --------------------------------------------------------------------------
# runtime
# --------------------------------------------------------------------------
FROM python:3.14-slim AS runtime

LABEL org.opencontainers.image.title="pg-operator" \
      org.opencontainers.image.description="Kubernetes operator for external PostgreSQL (RDS) instances" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.source="https://github.com/your-org/pg-operator"

# psycopg[binary] bundles libpq, so no PostgreSQL client libraries are needed.
# ca-certificates is required for TLS to both the Kubernetes API and RDS.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --gid 65532 nonroot \
 && useradd --uid 65532 --gid 65532 --no-create-home --shell /usr/sbin/nologin nonroot

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PG_OPERATOR_LOG_FORMAT=json

WORKDIR /app
COPY --from=builder --chown=root:root /app/.venv /app/.venv
COPY --from=builder --chown=root:root /app/src /app/src

USER 65532:65532

# Liveness (operator) and the state API.
EXPOSE 8080 8000 9090

# Overridden by the chart, which adds the watch scope and peering flags.
ENTRYPOINT []
CMD ["kopf", "run", "--module=pg_operator.operator", "--all-namespaces", \
     "--liveness=http://0.0.0.0:8080/healthz"]
