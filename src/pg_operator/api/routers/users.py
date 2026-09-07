"""PostgresUser views.

Credential values are never exposed: a user response names the Secret and the
keys it holds, so a consumer can find the credential without this API becoming a
way to read it.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, status

from ...api.deps import StoreDep
from ...api.schemas import UserSummary

router = APIRouter(prefix="/users", tags=["users"])


@router.get("", summary="List users", response_model=list[UserSummary])
async def list_users(
    store: StoreDep,
    namespace: str | None = Query(default=None),
    instance: str | None = Query(default=None),
    database: str | None = Query(default=None, description="Filter by granted database"),
    access: str | None = Query(default=None, description="OWNER, RW or RO"),
    phase: str | None = Query(default=None),
) -> list[UserSummary]:
    items = await store.users(namespace)
    if instance:
        items = [item for item in items if item.instance == instance]
    if database:
        items = [
            item
            for item in items
            if any(grant.database == database for grant in item.grants)
        ]
    if access:
        wanted = access.upper()
        items = [
            item for item in items if any(grant.role == wanted for grant in item.grants)
        ]
    if phase:
        items = [item for item in items if item.phase.lower() == phase.lower()]
    return sorted(items, key=lambda item: (item.namespace, item.name))


@router.get("/{namespace}/{name}", summary="Get one user", response_model=UserSummary)
async def get_user(namespace: str, name: str, store: StoreDep) -> UserSummary:
    user = await store.user(namespace, name)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"PostgresUser {namespace}/{name} not found",
        )
    return user
