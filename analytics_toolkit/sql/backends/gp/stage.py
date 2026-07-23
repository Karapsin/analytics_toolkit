from __future__ import annotations

import hashlib
from typing import Any

from analytics_toolkit.sql.backends.base import _apply_query_label

GP_IDENTIFIER_MAX_BYTES = 63


def build_materialize_transfer_source_sql(
    adapter: Any,
    table_name: str,
    source_sql: str,
    *,
    query_label: str | None = None,
) -> str:
    return _apply_query_label(
        f"CREATE TABLE {table_name} AS {adapter.strip_query_semicolon(source_sql)} "
        "DISTRIBUTED RANDOMLY",
        query_label,
    )


def stage_base_identifier(
    adapter: Any,
    base_identifier: str,
    transfer_staging_username: str | None,
    stage_suffix: str,
) -> str:
    del adapter
    marker = (
        f"__analytics_toolkit_{transfer_staging_username}__stage__"
        if transfer_staging_username
        else "__stage__"
    )
    max_base_bytes = GP_IDENTIFIER_MAX_BYTES - len(marker.encode()) - len(stage_suffix.encode())
    if max_base_bytes <= 0:
        raise ValueError("Stage table marker is too long for Greenplum identifiers.")
    return _fit_identifier_bytes(base_identifier, max_base_bytes)


def _fit_identifier_bytes(identifier: str, max_bytes: int) -> str:
    if len(identifier.encode()) <= max_bytes:
        return identifier

    digest = hashlib.sha1(identifier.encode()).hexdigest()[:8]
    digest_suffix = f"_{digest}"
    digest_bytes = len(digest_suffix.encode())
    if digest_bytes >= max_bytes:
        return digest[:max_bytes]

    prefix = _truncate_identifier_bytes(identifier, max_bytes - digest_bytes)
    return f"{prefix}{digest_suffix}"


def _truncate_identifier_bytes(identifier: str, max_bytes: int) -> str:
    encoded = identifier.encode()
    if len(encoded) <= max_bytes:
        return identifier
    return encoded[:max_bytes].decode(errors="ignore")
