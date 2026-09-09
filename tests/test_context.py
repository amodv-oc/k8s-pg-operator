"""Reconcile context plumbing: per-resource locking and capability caching."""

from __future__ import annotations

import asyncio

from pg_operator.config import Settings
from pg_operator.postgres.connection import Endpoint
from pg_operator.postgres.server import ServerInfo
from pg_operator.reconcile.context import ReconcileContext, ResourceLocks

from .fakes import FakeK8sClient


async def test_passes_over_one_resource_never_overlap() -> None:
    """A timer tick and a change handler for the same object must serialise.

    kopf runs them as independent tasks; without the lock both would issue DDL
    and rewrite status at once.
    """
    locks = ResourceLocks()
    active = 0
    peak = 0

    async def pass_() -> None:
        nonlocal active, peak
        async with locks.hold("PostgresDB", "team-a", "orders"):
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1

    await asyncio.gather(*(pass_() for _ in range(5)))
    assert peak == 1
    # Nothing is left behind once every holder has released.
    assert len(locks) == 0


async def test_different_resources_run_concurrently() -> None:
    """Serialising one resource must not serialise the whole operator."""
    locks = ResourceLocks()
    active = 0
    peak = 0

    async def pass_(name: str) -> None:
        nonlocal active, peak
        async with locks.hold("PostgresUser", "team-a", name):
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1

    await asyncio.gather(pass_("a"), pass_("b"), pass_("c"))
    assert peak == 3


async def test_lock_is_scoped_by_kind_and_namespace() -> None:
    """orders in team-a and orders in team-b are unrelated resources."""
    locks = ResourceLocks()
    async with locks.hold("PostgresDB", "team-a", "orders"):
        assert locks.is_held("PostgresDB", "team-a", "orders")
        assert not locks.is_held("PostgresDB", "team-b", "orders")
        assert not locks.is_held("PostgresUser", "team-a", "orders")
        # A second holder can take an unrelated key without waiting.
        async with locks.hold("PostgresDB", "team-b", "orders"):
            assert len(locks) == 2
    assert len(locks) == 0


async def test_lock_is_released_when_the_pass_raises() -> None:
    locks = ResourceLocks()
    try:
        async with locks.hold("PostgresDB", None, "cluster-thing"):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert not locks.is_held("PostgresDB", None, "cluster-thing")
    assert len(locks) == 0


def _info() -> ServerInfo:
    return ServerInfo(
        version_num=160003,
        version_text="PostgreSQL 16.3",
        current_user="pgadmin",
        is_superuser=False,
        can_create_db=True,
        can_create_role=True,
        is_rds_superuser=True,
    )


def _endpoint(instance: str) -> Endpoint:
    return Endpoint(
        instance=instance, host=f"{instance}.rds", port=5432, username="u", password="p"
    )


async def test_invalidate_forgets_only_the_named_instance() -> None:
    """A spec change on one server must not force every server to be re-probed."""
    ctx = ReconcileContext(FakeK8sClient(), settings=Settings())  # type: ignore[arg-type]
    prod, staging = _endpoint("prod"), _endpoint("staging")
    ctx._server_info[(prod.instance, prod.fingerprint)] = (1e12, _info())
    ctx._server_info[(staging.instance, staging.fingerprint)] = (1e12, _info())

    ctx.invalidate("prod")

    assert (prod.instance, prod.fingerprint) not in ctx._server_info
    assert (staging.instance, staging.fingerprint) in ctx._server_info
