from __future__ import annotations

from ..backends.base import BackendCapability, WriteMode
from ..backends.registry import (
    BACKEND_REGISTRY,
    backend_capability_map,
    get_backend_capability,
)
from ..execution.operation_runner import timed_public_sql_function


BACKEND_CAPABILITIES = backend_capability_map()


def validate_write_mode(
    connection_type_or_key: str,
    write_mode: str,
    *,
    option_name: str = "write_mode",
) -> WriteMode:
    normalized = write_mode.strip().lower()
    if normalized not in {"append", "replace", "truncate_insert", "upsert"}:
        raise ValueError(
            f"{option_name} must be one of: append, replace, truncate_insert, upsert."
        )

    capability = get_backend_capability(connection_type_or_key)
    if normalized not in capability.supported_write_modes:
        raise ValueError(
            f"{capability.display_name} does not support {option_name}={normalized!r}."
        )
    return normalized  # type: ignore[return-value]


@timed_public_sql_function
def support_matrix_rows() -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for backend_name in BACKEND_REGISTRY:
        capability = get_backend_capability(backend_name)
        rows.append(
            {
                "backend": capability.name,
                "name": capability.display_name,
                "dialect": capability.sqlglot_dialect,
                "transactions": _yes_no(capability.supports_transactions),
                "analyze": _yes_no(capability.supports_analyze),
                "distributed": _yes_no(capability.supports_distributed_tables),
                "write_modes": ", ".join(sorted(capability.supported_write_modes)),
                "truncate": capability.truncate_semantics,
            }
        )
    return rows


@timed_public_sql_function
def format_support_matrix() -> str:
    headers = [
        "Backend",
        "Dialect",
        "Transactions",
        "Analyze",
        "Distributed",
        "Write modes",
        "Truncate",
    ]
    rows = [
        [
            row["backend"],
            row["dialect"],
            row["transactions"],
            row["analyze"],
            row["distributed"],
            row["write_modes"],
            row["truncate"],
        ]
        for row in support_matrix_rows()
    ]
    widths = [
        max(len(headers[index]), *(len(row[index]) for row in rows))
        for index in range(len(headers))
    ]
    header_line = "  ".join(
        header.ljust(widths[index]) for index, header in enumerate(headers)
    )
    divider = "  ".join("-" * width for width in widths)
    body = [
        "  ".join(value.ljust(widths[index]) for index, value in enumerate(row))
        for row in rows
    ]
    return "\n".join([header_line, divider, *body])


def _yes_no(value: bool) -> str:
    return "yes" if value else "no"


__all__ = [
    "BACKEND_CAPABILITIES",
    "BackendCapability",
    "WriteMode",
    "format_support_matrix",
    "get_backend_capability",
    "support_matrix_rows",
    "validate_write_mode",
]
