"""Which selected tables a publication may carry, and why the others may not.

Pure: catalog facts in, verdicts out. Every check here guards against a real
failure - most of them against one that would land on the *source*: PostgreSQL
refuses UPDATE and DELETE on a published table whose replica identity cannot
identify the row, so publishing the wrong table takes the source application's
writes down. The rest stop a table the target cannot receive from stalling
apply for every other table.

A reason is ``source``- or ``target``-sided, because they are handled
differently once a table is already published (design D6): a source-side
failure takes the table out of the publication immediately - for the
replica-identity checks only that lets the source's writes through again, and
a table the source cannot replicate whole (row security, an unlogged leaf) is
not replicated at all rather than silently in part; a target-side failure
leaves it in, since the change that broke apply is already in the stream and
removing the table cannot recall it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ..models import ReplicationSpec, RowFilter
from ..postgres.publication import SourceTable, TargetTable

Side = Literal["source", "target"]

SOURCE_TABLE_MISSING = "SourceTableMissing"
NOT_PUBLISHABLE = "NotPublishable"
UNKNOWN_COLUMN = "UnknownColumn"
NO_REPLICA_IDENTITY = "NoReplicaIdentity"
ROW_FILTER_NOT_COVERED = "RowFilterNotCoveredByIdentity"
#: Also given to a REPLICA IDENTITY FULL table with any column list: PostgreSQL
#: allows none on such a table once UPDATE or DELETE is published.
COLUMN_LIST_MISSES_IDENTITY = "ColumnListMissesIdentity"
#: PostgreSQL 18 refuses UPDATE and DELETE on a table whose replica identity
#: takes in a generated column the publication does not carry - and the
#: operator never publishes generated columns.
GENERATED_COLUMN_IN_IDENTITY = "GeneratedColumnInIdentity"
FILTER_IGNORED_WITHOUT_ROOT = "FilterIgnoredWithoutPartitionRoot"
SOURCE_TABLE_NOT_OWNED = "SourceTableNotOwned"
SOURCE_TABLE_NOT_READABLE = "SourceTableNotReadable"
ROW_SECURITY_ENABLED = "RowSecurityEnabled"
TARGET_TABLE_MISSING = "TargetTableMissing"
TARGET_COLUMNS_MISSING = "TargetColumnsMissing"
TARGET_OWNER_NOT_SETTABLE = "TargetOwnerNotSettable"
#: The target cannot identify the row an UPDATE or DELETE names, which stops
#: apply for every table the subscription carries, not only this one.
TARGET_NO_REPLICA_IDENTITY = "TargetNoReplicaIdentity"
#: The target identifies rows by columns the publisher does not send as part of
#: *its* replica identity. Same consequence.
TARGET_IDENTITY_NOT_SENT = "TargetIdentityNotSent"
#: A NOT NULL column on the target that the publication does not carry and the
#: server cannot default. Every INSERT the subscription applies fails.
TARGET_COLUMN_REQUIRED = "TargetColumnRequired"
#: The initial copy of a newly subscribed table would collide with rows the
#: target already holds - most often a table unpublished (D6) and re-added.
TARGET_TABLE_NOT_EMPTY = "TargetTableNotEmpty"
TABLE_TARGETED_TWICE = "TableTargetedTwice"
#: The server refused the table's ADD TABLE - most often an operand that does
#: not convert to its column's type.
INVALID_ROW_FILTER = "InvalidRowFilter"
PUBLICATION_REFUSED = "PublicationRefused"
#: A warning, not a gate: apply converts through text, and widening a column
#: on the target (int -> bigint) is a deliberate, common choice.
COLUMN_TYPE_MISMATCH = "ColumnTypeMismatch"

#: pg_class.relkind values a publication can carry.
PUBLISHABLE_KINDS = frozenset({"r", "p"})

#: The first major version whose publisher refuses UPDATE and DELETE when the
#: replica identity holds an unpublished generated column.
GENERATED_IDENTITY_CHECK_VERSION = 18

_KIND_NAMES = {"v": "a view", "m": "a materialised view", "f": "a foreign table"}


@dataclass(frozen=True, slots=True)
class Candidate:
    """A table the spec selects, whole (from ``schemas``) or explicitly (``tables``)."""

    schema: str
    name: str
    #: Empty publishes every column.
    columns: tuple[str, ...] = ()
    row_filter: RowFilter | None = None
    explicit: bool = False

    @property
    def qualified(self) -> str:
        return f"{self.schema}.{self.name}"


@dataclass(frozen=True, slots=True)
class Issue:
    reason: str
    message: str
    side: Side

    def as_status(self, table: str) -> dict[str, str]:
        return {"table": table, "reason": self.reason, "message": self.message}


@dataclass(frozen=True, slots=True)
class Verdict:
    candidate: Candidate
    issues: tuple[Issue, ...] = ()
    warnings: tuple[Issue, ...] = ()

    @property
    def publishable(self) -> bool:
        return not self.issues

    @property
    def keeps_membership(self) -> bool:
        """Whether an already-published table should stay published despite its issues.

        Only target-side issues qualify. A table claimed by an older replication
        must go regardless: two subscriptions writing one table is the conflict
        the check exists to end.
        """
        return all(
            issue.side == "target" and issue.reason != TABLE_TARGETED_TWICE for issue in self.issues
        )


def candidates(spec: ReplicationSpec, source: dict[str, SourceTable]) -> list[Candidate]:
    """Expand ``schemas`` against the source catalog, then overlay ``tables``.

    A schema contributes its permanent tables and partitioned tables; leaf
    partitions are carried by their partitioned table, and views, foreign and
    unlogged tables are skipped quietly. An explicit entry is always a candidate,
    so naming something unpublishable is reported rather than ignored.
    """
    explicit = {entry.qualified for entry in spec.tables}
    found: dict[str, Candidate] = {}
    for selection in spec.schemas:
        excluded = set(selection.exclude_tables)
        for table in source.values():
            if (
                table.schema != selection.name
                or table.name in excluded
                or table.qualified in explicit
                or table.is_partition
                or table.kind not in PUBLISHABLE_KINDS
                or not table.persistent
            ):
                continue
            found[table.qualified] = Candidate(table.schema, table.name)
    for entry in spec.tables:
        found[entry.qualified] = Candidate(
            entry.schema_, entry.name, tuple(entry.columns), entry.row_filter, explicit=True
        )
    return [found[key] for key in sorted(found)]


def target_relations(source: SourceTable, *, via_root: bool) -> tuple[SourceTable, ...]:
    """What the subscription applies into: the table itself, or each leaf partition.

    Without ``publishViaPartitionRoot`` a partitioned table's changes are
    published under the names of the leaves they land in, so the target needs
    those leaves, not merely the parent.
    """
    if source.kind == "p" and not via_root:
        return source.leaves
    return (source,)


def judge(
    candidate: Candidate,
    source: SourceTable | None,
    targets: dict[str, TargetTable],
    *,
    keyed: bool,
    via_root: bool,
    owner_group: str,
    read_group: str,
    login: str,
    claimed_by: str | None = None,
    copy_data: bool = False,
    subscribed: frozenset[str] = frozenset(),
    target_nonempty: frozenset[str] = frozenset(),
    source_major_version: int = 16,
) -> Verdict:
    """Decide one candidate. ``keyed`` means UPDATE or DELETE is published.

    ``subscribed`` is what the subscription already carries; with
    ``copy_data``, any other relation gets an initial copy, which must land in
    an empty table. ``source_major_version`` is the source server's, since
    what its publisher enforces differs by release.
    """
    q = candidate.qualified
    if source is None:
        return Verdict(
            candidate,
            (_source(SOURCE_TABLE_MISSING, f"{q} does not exist in the source database"),),
        )
    if source.is_partition:
        return Verdict(
            candidate,
            (_source(NOT_PUBLISHABLE, f"{q} is a partition; select its partitioned table instead"),),
        )
    if source.kind not in PUBLISHABLE_KINDS or not source.persistent:
        what = _KIND_NAMES.get(source.kind, "unlogged or temporary")
        return Verdict(
            candidate,
            (_source(NOT_PUBLISHABLE, f"{q} is {what}; only permanent tables can be published"),),
        )

    issues: list[Issue] = []
    # A partitioned table is always permanent, but its leaves need not be. An
    # unlogged leaf writes no WAL, and a foreign one keeps its rows on another
    # server, so neither is ever decoded: the table would replicate only in
    # part, silently.
    unlogged = [leaf.qualified for leaf in source.leaves if leaf.kind == "r" and not leaf.persistent]
    if unlogged:
        issues.append(
            _source(
                NOT_PUBLISHABLE,
                f"{q} has unlogged partition(s) {', '.join(unlogged)}, whose changes are never "
                "written to the WAL and so never replicated; make them permanent with "
                "ALTER TABLE ... SET LOGGED",
            )
        )
    foreign = [leaf.qualified for leaf in source.leaves if leaf.kind != "r"]
    if foreign:
        issues.append(
            _source(
                NOT_PUBLISHABLE,
                f"{q} has foreign partition(s) {', '.join(foreign)}, whose rows live on another "
                "server and are never replicated; replace them with local tables, or "
                f"DETACH PARTITION them from {q}",
            )
        )
    filter_columns = candidate.row_filter.columns if candidate.row_filter else frozenset()
    unknown = sorted(
        column
        for column in {*candidate.columns, *filter_columns}
        if column not in source.columns
    )
    if unknown:
        generated = [column for column in unknown if column in source.generated]
        detail = f" ({', '.join(generated)} generated, which cannot be published)" if generated else ""
        issues.append(
            _source(UNKNOWN_COLUMN, f"{q} has no publishable column(s) {', '.join(unknown)}{detail}")
        )

    if source.kind == "p" and not via_root and (candidate.columns or candidate.row_filter):
        # Each leaf would be published under its own (empty) filter and column
        # list - so every row and column would flow, silently.
        issues.append(
            _source(
                FILTER_IGNORED_WITHOUT_ROOT,
                f"{q} is partitioned: its column list and row filter apply only with "
                "publishViaPartitionRoot: true; without it every row and column would be published",
            )
        )

    if keyed:
        # Only what the publication carries is checked: a leaf held back above
        # is never published, so the source enforces nothing on it.
        members = (
            tuple(leaf for leaf in source.leaves if _publishable(leaf))
            if source.kind == "p"
            else (source,)
        )
        issues.extend(
            _identity_issues(
                candidate,
                members,
                filter_columns,
                generated_checked=source_major_version >= GENERATED_IDENTITY_CHECK_VERSION,
            )
        )

    if not source.owner_usable:
        issues.append(
            _source(
                SOURCE_TABLE_NOT_OWNED,
                f"{q} is owned by {source.owner}, whose privileges the managing role does not "
                f"hold, so it cannot be added to a publication; ALTER TABLE {q} OWNER TO {owner_group}",
            )
        )
    unreadable = [t.qualified for t in (source, *source.leaves) if not t.readable_by_login]
    if unreadable:
        issues.append(
            _source(
                SOURCE_TABLE_NOT_READABLE,
                f"replication login {login} cannot SELECT {', '.join(unreadable)}, so the initial "
                f"copy would fail; GRANT SELECT to {read_group}",
            )
        )
    secured = [t.qualified for t in (source, *source.leaves) if t.row_security]
    if secured:
        issues.append(
            _source(
                ROW_SECURITY_ENABLED,
                f"{', '.join(secured)} has row-level security: the initial copy would see only "
                "the rows its policies allow while the change stream carries them all, leaving "
                "a silently partial replica",
            )
        )

    warnings: list[Issue] = []
    # A column the source lacks is already reported as unknown; counting it
    # missing on the target too would give one typo two causes.
    published_columns = tuple(
        column for column in candidate.columns or source.columns if column in source.columns
    )
    for relation in target_relations(source, via_root=via_root):
        target = targets.get(relation.qualified)
        if target is None or target.kind not in PUBLISHABLE_KINDS:
            issues.append(
                _target(
                    TARGET_TABLE_MISSING,
                    f"{relation.qualified} does not exist in the target database; create it there "
                    "and it is published on the next pass",
                )
            )
            continue
        missing = [column for column in published_columns if column not in target.columns]
        if missing:
            issues.append(
                _target(
                    TARGET_COLUMNS_MISSING,
                    f"{relation.qualified} on the target lacks published column(s) "
                    f"{', '.join(missing)} (or has them generated)",
                )
            )
        if (
            copy_data
            and relation.qualified not in subscribed
            and relation.qualified in target_nonempty
        ):
            issues.append(
                _target(
                    TARGET_TABLE_NOT_EMPTY,
                    f"{relation.qualified} already has rows on the target, and its initial copy "
                    "would collide with them; empty it there, or set copyData: false if they "
                    "are already in step with the source",
                )
            )
        if not target.owner_settable:
            issues.append(
                _target(
                    TARGET_OWNER_NOT_SETTABLE,
                    f"{relation.qualified} on the target is owned by {target.owner}, which the "
                    "managing role cannot SET ROLE to, and PostgreSQL 16 applies changes as the "
                    "table's owner",
                )
            )
        required = sorted(
            column for column in target.required_columns if column not in published_columns
        )
        if required:
            issues.append(
                _target(
                    TARGET_COLUMN_REQUIRED,
                    f"{relation.qualified} on the target requires column(s) {', '.join(required)}, "
                    "which this publication does not carry and the server cannot default: every "
                    "INSERT the subscription applies would fail. Give them a DEFAULT on the "
                    "target, drop their NOT NULL, or publish them",
                )
            )
        if keyed:
            issues.extend(_target_identity_issues(relation, target))
        differing = [
            f"{column} {source.columns[column]} -> {target.columns[column]}"
            for column in published_columns
            if column in target.columns
            and column in source.columns
            and source.columns[column] != target.columns[column]
        ]
        if differing:
            warnings.append(
                _target(
                    COLUMN_TYPE_MISMATCH,
                    f"{relation.qualified} column types differ ({'; '.join(differing)}); "
                    "values are converted through text on apply",
                )
            )

    if claimed_by:
        issues.append(
            _target(
                TABLE_TARGETED_TWICE,
                f"{q} is already replicated into the same target database by {claimed_by}",
            )
        )
    return Verdict(candidate, tuple(issues), tuple(warnings))


def _publishable(table: SourceTable) -> bool:
    return table.kind in PUBLISHABLE_KINDS and table.persistent


def _target_identity_issues(source: SourceTable, target: TargetTable) -> list[Issue]:
    """Whether the target can find the row an UPDATE or DELETE names.

    PostgreSQL 16's logical-replication documentation puts it as: "If a replica
    identity other than FULL is set on the publisher side, a replica identity
    comprising the same or fewer columns must also be set on the subscriber
    side." A publisher sending FULL rows therefore asks nothing of the target;
    anything else does, and failing it stops apply for the whole subscription -
    so this is a gate, not a warning, even though the fault is on the target.
    """
    if source.replica_identity == "f" or source.identity_columns is None:
        # Nothing is asked of the target when the publisher sends whole rows -
        # and when the *source* cannot identify a row either, NoReplicaIdentity
        # has already excluded the table, with the remedy at the end that needs it.
        return []
    if target.identity_columns is None:
        return [
            _target(
                TARGET_NO_REPLICA_IDENTITY,
                f"{target.qualified} on the target has no REPLICA IDENTITY index and no primary "  # noqa: S608 - status text, never executed
                "key the apply worker can use (a DEFERRABLE one does not count), so it could "
                "not find the rows to update or delete: it would stop the whole subscription, "
                "or on PostgreSQL 18 skip those changes when the key is deferrable; add a "
                f"primary key that is not DEFERRABLE there, or set REPLICA IDENTITY FULL on "
                f"{source.qualified} at the source",
            )
        ]
    # The target may identify rows by fewer columns than the publisher sends,
    # never by more - and what counts is the publisher's replica identity, not
    # merely which columns it publishes.
    uncovered = sorted(target.identity_columns - source.identity_columns)
    if uncovered:
        return [
            _target(
                TARGET_IDENTITY_NOT_SENT,
                f"{target.qualified} on the target identifies rows by {', '.join(uncovered)}, "
                f"which is outside the replica identity {source.qualified} publishes "
                f"({', '.join(sorted(source.identity_columns))}); the publisher never sends "
                "those columns as keys, so apply would stop for the whole subscription",
            )
        ]
    return []


def _identity_issues(
    candidate: Candidate,
    members: tuple[SourceTable, ...],
    filter_columns: frozenset[str],
    *,
    generated_checked: bool,
) -> list[Issue]:
    """Replica-identity checks; the source enforces each of them on every UPDATE and DELETE.

    ``generated_checked`` is whether the source is PostgreSQL 18 or later,
    whose publisher also refuses an identity holding a generated column.
    """
    issues: list[Issue] = []
    for member in members:
        identity = member.identity_columns
        if identity is None:
            issues.append(
                _source(
                    NO_REPLICA_IDENTITY,
                    f"{member.qualified} has no replica identity: publishing it would make every "  # noqa: S608 - status text, never executed
                    "UPDATE and DELETE on the source fail; add a primary key (one that is not "
                    "DEFERRABLE, which PostgreSQL never uses as an identity) or set REPLICA IDENTITY",
                )
            )
            continue
        uncovered = sorted(filter_columns - identity)
        if uncovered:
            issues.append(
                _source(
                    ROW_FILTER_NOT_COVERED,
                    f"the row filter on {member.qualified} reads {', '.join(uncovered)}, outside its "
                    "replica identity: the source would refuse UPDATE and DELETE on it",
                )
            )
        if generated_checked and not candidate.columns:
            # PostgreSQL 18's pub_contains_invalid_column: with FULL the whole
            # row is the identity, so any generated column counts; otherwise
            # only the identity index's own columns do. A stored one would need
            # publish_generated_columns = stored, which the operator never
            # sets, and a virtual one can never be published at all. With a
            # column list the server checks coverage instead, which
            # ColumnListMissesIdentity below already reports.
            in_identity = (
                member.generated if member.replica_identity == "f" else identity & member.generated
            )
            if in_identity:
                issues.append(
                    _source(
                        GENERATED_COLUMN_IN_IDENTITY,
                        f"{member.qualified}'s replica identity takes in generated column(s) "
                        f"{', '.join(sorted(in_identity))}, which the publication does not carry: "
                        "PostgreSQL 18 would refuse UPDATE and DELETE on it at the source; "
                        "identify rows by an index without them (REPLICA IDENTITY USING INDEX)",
                    )
                )
        if candidate.columns and member.replica_identity == "f":
            issues.append(
                _source(
                    COLUMN_LIST_MISSES_IDENTITY,
                    f"{member.qualified} has REPLICA IDENTITY FULL, which PostgreSQL allows no "
                    "column list with: the source would refuse UPDATE and DELETE on it; drop "
                    "the column list, or move it off FULL with REPLICA IDENTITY DEFAULT (given "
                    "a primary key) or REPLICA IDENTITY USING INDEX",
                )
            )
        elif candidate.columns:
            missing = sorted(identity - set(candidate.columns))
            if missing:
                issues.append(
                    _source(
                        COLUMN_LIST_MISSES_IDENTITY,
                        f"the column list for {member.qualified} omits replica-identity column(s) "
                        f"{', '.join(missing)}: the source would refuse UPDATE and DELETE on it",
                    )
                )
    return issues


def _source(reason: str, message: str) -> Issue:
    return Issue(reason, message, "source")


def _target(reason: str, message: str) -> Issue:
    return Issue(reason, message, "target")
