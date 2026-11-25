"""Retain the complete dataframe certified before destination publication."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from analytics_toolkit.sql.backends.dataframe_types import inferred_dataframe_values
from analytics_toolkit.sql.backends.native_values import requires_native_assignment

if TYPE_CHECKING:
    import pandas as pd

    from .models import LoadOptions, LoadState


def prepare_owned_load_dataframe(
    options: LoadOptions, state: LoadState, frame: pd.DataFrame
) -> pd.DataFrame:
    """Snapshot known values; native opaque values are certified by their SQL checkpoint."""
    from analytics_toolkit.sql.core.exact_values import (  # noqa: PLC0415 - shared snapshot producer.
        freeze_dataframe_values,
    )

    cache = options.destination_context
    if "owned_load_inferred_schema" in cache:
        cache["inferred_schema"] = dict(cache["owned_load_inferred_schema"])
        cache["inferred_type_metadata"] = list(cache["owned_load_inferred_metadata"])
    if "native_assignment_configuration_digest" in cache:
        from .native_assignment import (  # noqa: PLC0415 -- native checkpoints import this module.
            freeze_assignment_identity,
        )

        freeze_assignment_identity(options)
    owned = cache.get("owned_load_dataframe")
    if owned is None:
        if (
            options.table_schema is None
            and not state.original_target_exists
            and "inferred_column_conversions" in cache
        ):
            cache["owned_load_inferred_schema"] = dict(cache["inferred_schema"])
            cache["owned_load_inferred_metadata"] = [
                dict(item) for item in cache["inferred_type_metadata"]
            ]
            frame = inferred_dataframe_values(frame, cache["inferred_column_conversions"])
        schema = (
            options.table_schema or state.target_column_types or cache.get("inferred_schema") or {}
        )
        opaque = tuple(
            name
            for name, native in schema.items()
            if requires_native_assignment(native, options.connection_backend)
        )
        owned = freeze_dataframe_values(frame, opaque_columns=opaque)
        cache["owned_load_dataframe"] = owned
    state.prepared_json_dataframe = cast("pd.DataFrame", owned)
    return state.prepared_json_dataframe


def inferred_load_schema(options: LoadOptions) -> dict[str, str] | None:
    schema = options.destination_context.get("owned_load_inferred_schema")
    return None if schema is None else dict(schema)


def inferred_load_metadata(
    options: LoadOptions, metadata: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    source = options.destination_context.get("owned_load_inferred_metadata")
    if source is None:
        return metadata
    current = {item["column"]: item for item in metadata}
    return [{**current.get(item["column"], {}), **item} for item in source]


def retain_prepared_load_error(error: BaseException, state: LoadState) -> None:
    """Expose retained recovery names without replacing the original interruption."""
    resources = tuple(
        dict.fromkeys(resource for resource, _stage, _policy in state.owned_stage_resources)
    )
    if resources:
        error.__dict__.setdefault("analytics_toolkit_sql_acknowledged_resources", resources)
    if state.upsert_checkpoint.prepared:
        error.__dict__["analytics_toolkit_sql_prepared_payload"] = (
            state.upsert_checkpoint.final_stage_table
        )
