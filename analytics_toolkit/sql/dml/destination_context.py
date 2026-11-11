"""Freeze target codec policy without altering authoritative SQL column types."""

from __future__ import annotations

from typing import Any, cast

from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.dataframe_types import _infer_column_type
from analytics_toolkit.sql.backends.temporal_context import TIMEZONE_POLICY_VERSION
from analytics_toolkit.sql.connection.errors import InvalidSqlInputError


def freeze_destination_timezone(
    cache: dict[str, Any],
    backend: str,
    connection: Any,
    schema: dict[str, str] | None = None,
    *,
    required: bool = False,
) -> str | None:
    previous_version = cache.get("timezone_policy_version", TIMEZONE_POLICY_VERSION)
    if previous_version != TIMEZONE_POLICY_VERSION:
        message = "Destination timestamp timezone policy differs from certified input."
        raise InvalidSqlInputError(message)
    cache["timezone_policy_version"] = TIMEZONE_POLICY_VERSION
    adapter = get_backend_adapter(backend)
    required = (
        required
        or adapter.requires_destination_timezone(schema or {})
        or cache.get("destination_timezone") is not None
    )
    zone = cast("str | None", adapter.destination_write_timezone(connection, required=required))
    if required and zone is None:
        message = (
            "Cannot resolve the destination's implicit timestamp write timezone before mutation."
        )
        raise InvalidSqlInputError(message)
    previous = cache.get("destination_timezone")
    if previous is not None and previous != zone:
        message = "Destination timestamp write timezone differs from certified input."
        raise InvalidSqlInputError(message)
    if zone is not None or "destination_timezone" not in cache:
        cache["destination_timezone"] = zone
    return zone


def infer_dataframe_write_schema(df: Any, backend: str, cache: dict[str, Any]) -> dict[str, str]:
    if "inferred_schema" not in cache:
        zone = cache.get("destination_timezone")
        schema = {
            str(name): _infer_column_type(df.iloc[:, index], backend, destination_timezone=zone)
            for index, name in enumerate(df.columns)
        }
        cache["inferred_schema"] = schema
    return dict(cache["inferred_schema"])


def prepare_dataframe_write_context(
    options: Any, state: Any, connection: Any, df: Any
) -> str | None:
    cache = options.destination_context
    schema = options.table_schema or state.target_column_types
    zone = freeze_destination_timezone(
        cache,
        options.connection_backend,
        connection,
        schema,
        required=schema is None
        and get_backend_adapter(options.connection_backend).requires_dataframe_timezone(df),
    )
    if schema is None:
        schema = infer_dataframe_write_schema(df, options.connection_backend, cache)
        adapter = get_backend_adapter(options.connection_backend)
        cache["context_inferred_schema"] = adapter.requires_dataframe_timezone(
            df
        ) or adapter.requires_destination_timezone(schema)
        zone = freeze_destination_timezone(cache, options.connection_backend, connection, schema)
        # A lazy native handshake can establish the effective zone on this pass.
        cache.pop("inferred_schema", None)
        infer_dataframe_write_schema(df, options.connection_backend, cache)
    return zone
