"""PostgresInstance views."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, status

from ...api.deps import StoreDep
from ...api.schemas import InstanceDetail, InstanceSummary

router = APIRouter(prefix="/instances", tags=["instances"])


@router.get("", summary="List instances", response_model=list[InstanceSummary])
async def list_instances(
    store: StoreDep,
    phase: str | None = Query(default=None, description="Filter by status phase"),
) -> list[InstanceSummary]:
    items = await store.instances()
    if phase:
        items = [item for item in items if item.phase.lower() == phase.lower()]
    return sorted(items, key=lambda item: item.name)


@router.get("/{name}", summary="Get one instance", response_model=InstanceDetail)
async def get_instance(name: str, store: StoreDep) -> InstanceDetail:
    """An instance plus every database and user that references it."""
    instance = await store.instance(name)
    if instance is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"PostgresInstance {name!r} not found",
        )
    databases = [db for db in await store.databases() if db.instance == name]
    users = [user for user in await store.users() if user.instance == name]
    return InstanceDetail(
        **instance.model_dump(),
        database_list=sorted(databases, key=lambda d: (d.namespace, d.name)),
        user_list=sorted(users, key=lambda u: (u.namespace, u.name)),
    )
