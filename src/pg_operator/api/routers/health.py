"""Liveness, readiness and health detail."""

from __future__ import annotations

from fastapi import APIRouter, Response, status

from ...api.deps import ProviderDep
from ...api.schemas import Health

router = APIRouter(tags=["health"])


@router.get("/healthz", summary="Liveness probe")
async def healthz() -> dict[str, str]:
    """Always answers while the process is up; does not touch Kubernetes."""
    return {"status": "ok"}


@router.get("/readyz", summary="Readiness probe", response_model=Health)
async def readyz(provider: ProviderDep, response: Response) -> Health:
    """Ready only when the Kubernetes API answers and the CRDs are installed."""
    try:
        store = await provider.store()
    except Exception as exc:  # noqa: BLE001 - any failure means not ready
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return Health(
            status="degraded",
            kubernetes=False,
            detail=f"cannot reach the Kubernetes API: {exc}",
        )

    health = await store.health()
    if health.status != "ok":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return health
