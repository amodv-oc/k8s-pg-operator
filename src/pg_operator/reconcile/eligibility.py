"""Which selected tables a publication may carry, and why the others may not.

Pure: catalog facts in, verdicts out. Every check here guards against a real
failure - most of them against one that would land on the *source*: PostgreSQL
refuses UPDATE and DELETE on a published table whose replica identity cannot
identify the row, so publishing the wrong table takes the source application's
writes down. The rest stop a table the target cannot receive from stalling
apply for every other table.

A reason is ``source``- or ``target``-sided, because they are handled
differently once a table is already published (design D6): a source-side
failure takes the table out of the publication immediately, since only that
lets the source's writes through again; a target-side failure leaves it in,
since the change that broke apply is already in the stream and removing the
table cannot recall it.
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
COLUMN_LIST_MISSES_IDENTITY = "ColumnListMissesIdentity"
FILTER_IGNORED_WITHOUT_ROOT = "FilterIgnoredWithoutPartitionRoot"
SOURCE_TABLE_NOT_OWNED = "SourceTableNotOwned"
SOURCE_TABLE_NOT_READABLE = "SourceTableNotReadable"
ROW_SECURITY_ENABLED = "RowSecurityEnabled"
TARGET_TABLE_MISSING = "TargetTableMissing"
TARGET_COLUMNS_MISSING = "TargetColumnsMissing"
TARGET_OWNER_NOT_SETTABLE = "TargetOwnerNotSettable"
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
) -> Verdict:
    """Decide one candidate. ``keyed`` means UPDATE or DELETE is published."""
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
        members = source.leaves if source.kind == "p" else (source,)
        issues.extend(_identity_issues(candidate, members, filter_columns))

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
        if not target.owner_settable:
            issues.append(
                _target(
                    TARGET_OWNER_NOT_SETTABLE,
                    f"{relation.qualified} on the target is owned by {target.owner}, which the "
                    "managing role cannot SET ROLE to, and PostgreSQL 16 applies changes as the "
                    "table's owner",
                )
            )
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


def _identity_issues(
    candidate: Candidate, members: tuple[SourceTable, ...], filter_columns: frozenset[str]
) -> list[Issue]:
    """Replica-identity checks; the source enforces each of them on every UPDATE and DELETE."""
    issues: list[Issue] = []
    for member in members:
        identity = member.identity_columns
        if identity is None:
            issues.append(
                _source(
                    NO_REPLICA_IDENTITY,
                    f"{member.qualified} has no replica identity: publishing it would make every "  # noqa: S608 - status text, never executed
                    "UPDATE and DELETE on the source fail; add a primary key or set REPLICA IDENTITY",
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
        if candidate.columns:
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
