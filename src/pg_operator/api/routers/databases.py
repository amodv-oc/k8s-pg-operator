"""PostgresDB views."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, status

from ...api.deps import StoreDep
from ...api.schemas import DatabaseDetail, DatabaseSummary

router = APIRouter(prefix="/databases", tags=["databases"])


@router.get("", summary="List databases", response_model=list[DatabaseSummary])
async def list_databases(
    store: StoreDep,
    namespace: str | None = Query(default=None),
    instance: str | None = Query(default=None),
    phase: str | None = Query(default=None),
    drifted: bool | None = Query(
        default=None, description="Only databases holding retained orphaned objects"
    ),
) -> list[DatabaseSummary]:
    items = await store.databases(namespace)
    if instance:
        items = [item for item in items if item.instance == instance]
    if phase:
        items = [item for item in items if item.phase.lower() == phase.lower()]
    if drifted is not None:
        items = [
            item
            for item in items
            if bool(item.orphaned_schemas or item.orphaned_extensions) is drifted
        ]
    return sorted(items, key=lambda item: (item.namespace, item.name))


@router.get(
    "/{namespace}/{name}", summary="Get one database", response_model=DatabaseDetail
)
async def get_database(namespace: str, name: str, store: StoreDep) -> DatabaseDetail:
    """A database, the users granted access to it, and replications from or into it."""
    database = await store.database(namespace, name)
    if database is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"PostgresDB {namespace}/{name} not found",
        )
    reference = f"{namespace}/{name}"
    users = [
        user
        for user in await store.users()
        if any(grant.db_ref == reference for grant in user.grants)
    ]
    replications = [
        r
        for r in await store.replications()
        if reference in (r.source.postgres_db, r.target.postgres_db)
    ]
    return DatabaseDetail(
        **database.model_dump(),
        users=sorted(users, key=lambda u: (u.namespace, u.name)),
        replications=sorted(replications, key=lambda r: (r.namespace, r.name)),
    )
