"""What the logical replication probe reports, without a server."""

from __future__ import annotations

from dataclasses import replace

from pg_operator.postgres.replication import LogicalReplicationInfo

#: A PostgreSQL 16 RDS instance with rds.logical_replication on: nothing to fix.
READY = LogicalReplicationInfo(
    major_version=16,
    current_user="pgadmin",
    wal_level="logical",
    max_replication_slots=20,
    replication_slots_in_use=2,
    max_wal_senders=35,
    max_slot_wal_keep_size="-1",
    create_subscription="Assumed",
    replication_grant="rds_replication",
    is_rds=True,
)


def test_a_ready_server_reports_no_issues() -> None:
    assert READY.issues() == []
    assert READY.as_status()["issues"] == []


def test_a_version_below_16_is_named() -> None:
    issues = replace(READY, major_version=15).issues()
    assert issues == ["PostgreSQL 15 is below the 16 that replication requires"]


def test_rds_names_the_parameter_group_for_wal_level() -> None:
    (issue,) = replace(READY, wal_level="replica").issues()
    assert "rds.logical_replication = 1" in issue
    assert "reboot" in issue


def test_a_stock_server_names_wal_level_itself() -> None:
    (issue,) = replace(READY, wal_level="replica", is_rds=False).issues()
    assert "set wal_level = logical and restart" in issue


def test_exhausted_slots_matter_only_once_wal_is_logical() -> None:
    full = replace(READY, replication_slots_in_use=20)
    assert full.issues() == ["all 20 replication slots are in use"]
    # With wal_level below logical that remedy comes first; a full count of
    # physical slots is not what stands in the way.
    assert len(replace(full, wal_level="replica").issues()) == 1


def test_no_wal_senders_is_named() -> None:
    assert replace(READY, max_wal_senders=0).issues() == [
        "max_wal_senders is 0, so no subscriber can connect"
    ]


def test_a_server_that_cannot_subscribe_can_still_publish() -> None:
    (issue,) = replace(READY, create_subscription="Unavailable").issues()
    assert "cannot create subscriptions" in issue
    assert "replication target" in issue


def test_no_replication_grant_points_at_a_provisioned_login() -> None:
    (issue,) = replace(READY, replication_grant="none").issues()
    assert "source.credentialsSecretRef" in issue


def test_status_carries_the_raw_settings() -> None:
    status = READY.as_status()
    assert status["walLevel"] == "logical"
    assert status["maxSlotWalKeepSize"] == "-1"
    assert status["replicationSlotsInUse"] == 2
    assert status["createSubscription"] == "Assumed"
    assert status["replicationGrant"] == "rds_replication"
