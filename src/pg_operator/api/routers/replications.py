"""PostgresReplication views."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, status

from ...api.deps import StoreDep
from ...api.schemas import ReplicationDetail, ReplicationSummary

router = APIRouter(prefix="/replications", tags=["replications"])


@router.get("", summary="List replications", response_model=list[ReplicationSummary])
async def list_replications(
    store: StoreDep,
    namespace: str | None = Query(default=None),
    instance: str | None = Query(default=None, description="Either end's PostgresInstance"),
    database: str | None = Query(
        default=None, description="Either end's PostgresDB, as namespace/name, or its database name"
    ),
    phase: str | None = Query(default=None),
    state: str | None = Query(
        default=None, description="Initializing, Streaming, Lagging, Stalled, SlotLost or Disabled"
    ),
) -> list[ReplicationDetail]:
    items = await store.replications(namespace)
    if instance:
        items = [i for i in items if instance in (i.source.instance, i.target.instance)]
    if database:
        items = [
            i
            for i in items
            if database
            in (i.source.postgres_db, i.target.postgres_db, i.source.database, i.target.database)
        ]
    if phase:
        items = [i for i in items if i.phase.lower() == phase.lower()]
    if state:
        items = [i for i in items if (i.state or "").lower() == state.lower()]
    return sorted(items, key=lambda item: (item.namespace, item.name))


@router.get(
    "/{namespace}/{name}", summary="Get one replication", response_model=ReplicationDetail
)
async def get_replication(namespace: str, name: str, store: StoreDep) -> ReplicationDetail:
    """A replication with every table: subscribed and its sync state, or held back and why."""
    replication = await store.replication(namespace, name)
    if replication is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"PostgresReplication {namespace}/{name} not found",
        )
    return replication
