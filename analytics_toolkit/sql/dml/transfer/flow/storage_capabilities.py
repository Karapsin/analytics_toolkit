"""Certify native timestamp storage independently for SQL stages and destinations."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from analytics_toolkit.sql.backends.native_values import (
    require_timestamp_write_capabilities,
    timestamp_capability_plan_options,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from analytics_toolkit.sql.backends.value_profiles import ColumnProfile
    from analytics_toolkit.sql.core.replay_spool import ReplaySpool
    from analytics_toolkit.sql.core.type_resolution import ResolvedColumn
    from analytics_toolkit.sql.dml.transfer.runtime.models import (
        TransferOptions,
        TransferStageState,
    )


def cache_converted_profiles(
    options: TransferOptions,
    spool: ReplaySpool,
    resolutions: Sequence[ResolvedColumn],
    profiles: Sequence[ColumnProfile],
) -> None:
    """Retain logical facts before a Parquet stage changes physical column types."""
    if (
        not spool.certified
        or len(spool.columns) != len(profiles)
        or len(spool.columns) != len(resolutions)
    ):
        message = "Storage certification requires complete converted input profiles."
        raise ValueError(message)
    options.input_resolution.update(
        converted_profiles=dict(zip(spool.columns, profiles)),
        resolved_logical_schema=dict(
            zip(spool.columns, (resolution.target_type for resolution in resolutions))
        ),
    )
    options.input_resolution.pop("final_storage_profiles", None)


def _final_profiles(
    options: TransferOptions,
    schema: Mapping[str, str],
    logical_schema: Mapping[str, str],
    profiles: Mapping[str, ColumnProfile],
) -> Mapping[str, ColumnProfile]:
    if dict(schema) == dict(logical_schema):
        return profiles
    from analytics_toolkit.sql.backends.conversion_profiles import (  # noqa: PLC0415 -- only inspect a certified checkpoint when final casts need storage proof.
        validate_converted_rows,
    )

    # Query, load and transfer preparation initialize each other recursively.
    from .conversion_validation import (  # noqa: PLC0415
        resolve_final_conversions,
    )

    cached = options.input_resolution
    spool = cast("ReplaySpool | None", cached.get("spool"))
    if spool is None or not spool.certified:
        message = "Final timestamp storage certification requires complete frozen input."
        raise ValueError(message)
    key = (spool.identity, tuple(schema.items()), cached.get("destination_timezone"))
    previous = cached.get("final_storage_profiles")
    if previous is not None and previous[0] == key:
        return cast("Mapping[str, ColumnProfile]", previous[1])
    resolutions = cached["resolutions"]
    targets = resolve_final_conversions(
        cached["source_schema"],
        resolutions,
        [profiles[name] for name in spool.columns],
        backend=options.to_db_backend,
        existing_types=schema,
        destination_timezone=cached.get("destination_timezone"),
    )
    if targets is None:
        message = "Final timestamp storage certification requires resolved native conversions."
        raise ValueError(message)
    converted_rows = (
        tuple(stage.convert(value) for stage, value in zip(resolutions, row))
        for row in spool.iter_rows()
    )
    final = dict(
        zip(
            spool.columns,
            validate_converted_rows(
                converted_rows,
                targets,
                backend=options.to_db_backend,
                destination_timezone=cached.get("destination_timezone"),
            ),
        )
    )
    cached["final_storage_profiles"] = (key, final)
    return final


def _stage_tables(options: TransferOptions, state: TransferStageState) -> list[tuple[str, bool]]:
    existing = state.stage_tables or ([state.stage_table] if state.stage_table is not None else [])
    if existing:
        return [(table, False) for table in dict.fromkeys(existing)]
    # Stage creation imports DDL/query orchestration that also imports this module.
    from analytics_toolkit.sql.dml.load.stage import (  # noqa: PLC0415
        build_stage_table_name,
    )

    return [
        (
            build_stage_table_name(
                options.to_db_backend,
                options.target_table,
                transfer_staging_schema=options.transfer_staging_schema,
                transfer_staging_username=options.transfer_staging_username,
                random_suffix=f"{options.transfer_id or 'storage'}__w00000",
                destination_hash=options.destination_hash,
            ),
            True,
        )
    ]


def require_storage_capabilities(
    options: TransferOptions,
    state: TransferStageState,
    connection: Any,
    *,
    query_input: bool = False,
    created_target: bool = False,
) -> None:
    """Use actual namespaces and resolved DDL properties before destination mutation.

    The backend owns connector and format discovery. Ordinary values and other
    backends retain their metadata-only transfer path without extra SQL.
    """
    if options.to_db_backend != "trino":
        return
    cached = options.input_resolution
    profiles = cast("Mapping[str, ColumnProfile] | None", cached.get("converted_profiles"))
    logical_schema = cast("Mapping[str, str] | None", cached.get("resolved_logical_schema"))
    if profiles is None or logical_schema is None:
        return
    receipts = cached.setdefault("timestamp_storage_receipts", {})
    if (
        not query_input
        and options.trino_mode != "parquet"
        and timestamp_capability_plan_options("trino", logical_schema, profiles)
    ):
        for table, creating in _stage_tables(options, state):
            receipts[("stage", table)] = require_timestamp_write_capabilities(
                "trino",
                connection,
                table,
                logical_schema,
                profiles,
                cached,
                creating=creating,
                properties=options.staging_ddl_properties,
            )
    final_schema = state.insert_column_types or logical_schema
    if not timestamp_capability_plan_options("trino", final_schema):
        return
    final_profiles = _final_profiles(options, final_schema, logical_schema, profiles)
    if not timestamp_capability_plan_options("trino", final_schema, final_profiles):
        return
    if query_input:
        creating = not created_target
    else:
        creating = not state.target_exists or (
            options.replace_target_table
            and options.write_mode == "replace"
            and not state.target_created_by_operation
        )
    receipts[("target", options.target_table)] = require_timestamp_write_capabilities(
        "trino",
        connection,
        options.target_table,
        final_schema,
        final_profiles,
        cached,
        creating=creating,
        properties=options.regular_ddl_properties,
    )


__all__ = ["cache_converted_profiles", "require_storage_capabilities"]
