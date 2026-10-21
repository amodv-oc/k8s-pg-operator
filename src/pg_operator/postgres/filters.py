"""Row filters: the spec's structured predicates, composed as SQL.

A publication's ``WHERE`` clause is DDL the master user runs, so it is built
here from parts that cannot carry SQL of their own: every column is a
``sql.Identifier``, every operand a ``sql.Literal``, and every operator one of
the fixed fragments below. Nothing from the spec is ever spliced in as text.
"""

from __future__ import annotations

from psycopg import sql

from ..models import FilterPredicate, RowFilter

_COMPARISONS = {
    "Eq": sql.SQL("="),
    "Ne": sql.SQL("<>"),
    "Lt": sql.SQL("<"),
    "Le": sql.SQL("<="),
    "Gt": sql.SQL(">"),
    "Ge": sql.SQL(">="),
}
_CONJUNCTIONS = {"AND": sql.SQL(" AND "), "OR": sql.SQL(" OR ")}


def render_row_filter(row_filter: RowFilter) -> sql.Composed:
    """The filter as one parenthesised expression, for ``WHERE {}``.

    An operand is a literal of unknown type, so PostgreSQL coerces it to the
    column's own type: ``'100'`` compares with an integer column as 100.
    """
    joiner = _CONJUNCTIONS[row_filter.conjunction]
    return sql.SQL("({})").format(
        joiner.join(_render_predicate(predicate) for predicate in row_filter.predicates)
    )


def _render_predicate(predicate: FilterPredicate) -> sql.Composed:
    column = sql.Identifier(predicate.column)
    if predicate.op == "IsNull":
        return sql.SQL("({} IS NULL)").format(column)
    if predicate.op == "IsNotNull":
        return sql.SQL("({} IS NOT NULL)").format(column)
    if predicate.op == "In":
        operands = sql.SQL(", ").join(sql.Literal(value) for value in predicate.values or [])
        return sql.SQL("({} IN ({}))").format(column, operands)
    return sql.SQL("({} {} {})").format(
        column, _COMPARISONS[predicate.op], sql.Literal(predicate.value)
    )
