"""Own only JSON payload columns while preserving other dataframe values."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from analytics_toolkit.sql.backends import get_backend_adapter

if TYPE_CHECKING:
    from collections.abc import Mapping

    import pandas as pd

    from .models import LoadOptions, LoadState


def prepare_json_dataframe(
    df: pd.DataFrame, schema: Mapping[str, str] | None, backend: str
) -> pd.DataFrame | None:
    """Freeze custom serializers once; already prepared documents are idempotent."""
    return get_backend_adapter(backend).prepare_dataframe_payload(df, schema)


def prepare_initial_json_dataframe(
    df: pd.DataFrame, schema: Mapping[str, str] | None, backend: str
) -> tuple[pd.DataFrame, pd.DataFrame | None]:
    """Prepare explicit JSON before planning and retain its reusable payload."""
    prepared = prepare_json_dataframe(df, schema, backend)
    return (prepared if prepared is not None else df), prepared


def attach_json_dataframe(
    state: LoadState, prepared: pd.DataFrame | None, df: pd.DataFrame
) -> pd.DataFrame:
    """Attach only the certified payload to a fresh disposable target state."""
    state.prepared_json_dataframe = prepared
    return prepared if prepared is not None else df


def convert_prepared_load_dataframe(
    options: LoadOptions, state: LoadState, df: pd.DataFrame
) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    """Convert transport input from the retained JSON documents, when present."""
    from analytics_toolkit.sql.dml.destination_context import (  # noqa: PLC0415
        infer_dataframe_write_schema,
    )

    from .type_validation import convert_dataframe_schema  # noqa: PLC0415 - import cycle

    prepared = state.prepared_json_dataframe
    frame = prepared if prepared is not None else df
    input_df = (
        frame.loc[:, list(state.target_column_types or {})]
        if options.write_mode == "upsert" and state.original_target_exists
        else frame
    )
    schema = (
        options.table_schema
        or state.target_column_types
        or infer_dataframe_write_schema(df, options.connection_backend, options.destination_context)
    )
    return convert_dataframe_schema(
        input_df,
        schema,
        options.connection_backend,
        destination_timezone=options.destination_context.get("destination_timezone"),
    )


def retain_json_dataframe(
    state: LoadState | None, prepared: pd.DataFrame | None
) -> pd.DataFrame | None:
    """Keep JSON documents while ordinary retry disposes its state and stages."""
    if state is not None and state.prepared_json_dataframe is not None:
        return state.prepared_json_dataframe
    return prepared
