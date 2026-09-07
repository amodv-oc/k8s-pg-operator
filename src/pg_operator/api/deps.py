"""FastAPI dependency wiring for the state store."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, Request, status

from .provider import ClientProvider
from .store import StateStore


def get_provider(request: Request) -> ClientProvider:
    provider: ClientProvider = request.app.state.provider
    return provider


async def get_store(request: Request) -> StateStore:
    """The state store, or 503 with the reason the cluster is unreachable."""
    provider = get_provider(request)
    try:
        return await provider.store()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Kubernetes API unavailable: {exc}",
        ) from exc


StoreDep = Annotated[StateStore, Depends(get_store)]
ProviderDep = Annotated[ClientProvider, Depends(get_provider)]
