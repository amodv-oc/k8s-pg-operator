"""Aggregate dashboard views."""

from __future__ import annotations

from fastapi import APIRouter

from ...api.deps import StoreDep
from ...api.schemas import Overview

router = APIRouter(tags=["overview"])


@router.get("/overview", summary="Aggregate state", response_model=Overview)
async def overview(store: StoreDep) -> Overview:
    """Phase counts across all three kinds, plus anything needing attention."""
    return await store.overview()
