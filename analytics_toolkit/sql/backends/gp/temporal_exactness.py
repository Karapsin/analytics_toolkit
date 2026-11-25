"""Certify final temporal narrowing against the actual requested native stage."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from analytics_toolkit.sql.backends.execution_outcome import close_cursor
from analytics_toolkit.sql.execution.cancellation import raise_if_cancelled
from analytics_toolkit.sql.execution.labels import apply_query_label

from .native_assignment import (
    NativeAssignmentContext,
    NativeAssignmentError,
    native_assignment_errors,
    require_native_assignment_context,
)
from .type_identity import canonical_builtin_type

if TYPE_CHECKING:
    from collections.abc import Mapping


def _family(native: str) -> tuple[str, bool, bool] | None:
    array = native.endswith("[]")
    while native.endswith("[]"):
        native = native[:-2].rstrip()
    for family in ("TIMESTAMP", "TIME", "INTERVAL", "DATE"):
        if native == family or native.startswith((family + "(", family + " ")):
            return family, "WITH TIME ZONE" in native, array
    return None


def _interval_difference(source: str, converted: str) -> str:
    # Native interval equality uses a 30-day month approximation. Compare
    # independent components so cancellation across month/day fields cannot
    # certify a changed calendar interval as equal.
    expressions = (
        "EXTRACT(YEAR FROM {value}) * 12 + EXTRACT(MONTH FROM {value})",
        "EXTRACT(DAY FROM {value})",
        "EXTRACT(HOUR FROM {value})",
        "EXTRACT(MINUTE FROM {value})",
        "EXTRACT(SECOND FROM {value})",
    )
    return " OR ".join(
        f"(({part.format(value=source)}) IS DISTINCT FROM ({part.format(value=converted)}))"
        for part in expressions
    )


def _interval_array_difference(source: str, converted: str) -> str:
    components = _interval_difference('"__atk_before"."value"', '"__atk_after"."value"')
    return (
        f"(array_dims({source}) IS DISTINCT FROM array_dims({converted})) OR "  # noqa: S608 -- generated quoted columns and checked type casts.
        f"EXISTS (SELECT 1 FROM unnest({source}) WITH ORDINALITY "
        'AS "__atk_before"("value", "position") FULL JOIN '
        f"unnest({converted}) WITH ORDINALITY "
        'AS "__atk_after"("value", "position") '
        'ON "__atk_before"."position" = "__atk_after"."position" '
        f"WHERE ({components}))"
    )


def validate_temporal_assignment_exactness(  # noqa: PLR0913 -- two complete schema authorities and frozen policy.
    adapter: Any,
    connection: Any,
    stage_table: str,
    source_types: Mapping[str, str],
    final_types: Mapping[str, str],
    *,
    query_label: str | None = None,
    session_context: NativeAssignmentContext | None = None,
) -> None:
    """Reject extra final loss after requested rounding has actually completed.

    This predicate augments native assignment; it never substitutes for a fully
    populated destination-typed checkpoint and its stored count/key certificate.
    """
    from analytics_toolkit.sql.backends import type_resolution  # noqa: PLC0415 -- resolver cycle.
    from analytics_toolkit.sql.core.identifiers import TableIdentifier  # noqa: PLC0415

    predicates = []
    for name, source in source_types.items():
        final = final_types.get(name)
        if final is None:
            continue
        original = canonical_builtin_type(source)
        target = canonical_builtin_type(final)
        if original is None or target is None or _family(original) != _family(target):
            continue
        kind = _family(original)
        if kind is None or type_resolution.declared_domain_fits(
            original, target, "gp", source_backend="gp"
        ):
            continue
        column = (
            adapter.quote_identifier("__atk_exact_input") + "." + adapter.quote_identifier(name)
        )
        converted = f"CAST({column} AS {target})"
        predicates.append(
            _interval_array_difference(column, converted)
            if kind[0] == "INTERVAL" and kind[2]
            else _interval_difference(column, converted)
            if kind[0] == "INTERVAL"
            else f"({column} IS DISTINCT FROM {converted})"
        )
    if not predicates:
        return
    table = TableIdentifier.parse(stage_table, "gp").render_quoted("gp")
    query = apply_query_label(
        f'SELECT 1 FROM {table} AS "__atk_exact_input" '  # noqa: S608 -- structured identifiers and canonical type identities.
        f"WHERE ({' OR '.join(predicates)}) LIMIT 1",
        query_label,
    )
    with native_assignment_errors("validation"):
        if session_context is None:
            message = "Final temporal exactness requires its frozen assignment session context."
            raise NativeAssignmentError(message)
        require_native_assignment_context(
            adapter, connection, session_context, query_label=query_label
        )
        raise_if_cancelled()
        cursor = connection.cursor()
        try:
            cursor.execute(query)
            mismatch = cursor.fetchone()
        finally:
            close_cursor(cursor, backend="gp")
        raise_if_cancelled()
        if mismatch is not None:
            message = "Requested native assignment exceeds the exact final temporal precision."
            raise NativeAssignmentError(message)
