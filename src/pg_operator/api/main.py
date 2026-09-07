"""FastAPI application exposing the operator's state.

Read-only by design in this iteration: it projects what the reconciler has
already written to resource status. Later iterations add mutating endpoints and
a React frontend, which is why the response models are a stable projection
rather than raw custom resources.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .. import __version__
from ..config import get_settings
from ..errors import OperatorError
from ..logging_setup import configure_logging
from .provider import ClientProvider
from .routers import databases, health, instances, overview, users

log = logging.getLogger(__name__)

DESCRIPTION = """
State API for pg-operator.

Reports what the operator has reconciled onto external PostgreSQL (RDS)
instances: instances and their reachability, databases with their schemas and
group roles, and users with their grants.

Credential values are never returned. A user response names the Secret holding
its credentials and the keys inside it, not the values.
"""


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Prepare the client provider for the app's lifetime.

    The Kubernetes client is created on first use, not here, so an unreachable
    cluster surfaces on /readyz rather than as a crash loop.
    """
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    app.state.settings = settings
    app.state.provider = ClientProvider(settings)
    log.info("state API ready (cache TTL: %ds)", settings.api_cache_ttl)
    try:
        yield
    finally:
        await app.state.provider.close()
        log.info("state API stopped")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="pg-operator state API",
        description=DESCRIPTION,
        version=__version__,
        lifespan=lifespan,
        root_path=settings.api_root_path,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )

    if settings.api_cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.api_cors_origins,
            allow_credentials=True,
            allow_methods=["GET", "OPTIONS"],
            allow_headers=["*"],
        )

    app.include_router(health.router)
    app.include_router(instances.router, prefix="/api/v1")
    app.include_router(databases.router, prefix="/api/v1")
    app.include_router(users.router, prefix="/api/v1")
    app.include_router(overview.router, prefix="/api/v1")

    @app.exception_handler(OperatorError)
    async def operator_error_handler(
        request: Request, exc: OperatorError
    ) -> JSONResponse:
        del request
        return JSONResponse(status_code=502, content={"detail": str(exc)})

    @app.get("/", include_in_schema=False)
    async def root() -> dict[str, str]:
        return {
            "name": "pg-operator state API",
            "version": __version__,
            "docs": "/docs",
            "overview": "/api/v1/overview",
        }

    return app


app = create_app()
