"""Certify owned client values before changing a dataframe destination."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from analytics_toolkit.sql.backends.client_validation import (
    supports_client_validation,
    validate_client_write_capabilities,
)
from analytics_toolkit.sql.core.exact_values import (
    dataframe_from_columns,
    decode_value,
    encode_value,
)

from .native_assignment import freeze_assignment_identity, require_assignment_identity

if TYPE_CHECKING:
    from collections.abc import Callable

    import pandas as pd

    from analytics_toolkit.sql.backends.ch.client_validation import ClientWriteValidation

    from .models import LoadOptions, LoadState


def client_validation_pending(options: LoadOptions, df: pd.DataFrame) -> bool:
    return supports_client_validation(options.connection_backend) and not df.empty


def client_creation_schema(options: LoadOptions) -> dict[str, str] | None:
    schema = options.destination_context.get("client_inferred_schema")
    return dict(cast("dict[str, str]", schema)) if isinstance(schema, dict) else None


def prepare_load_client_values(
    options: LoadOptions,
    state: LoadState,
    connection: Any,
    df: pd.DataFrame,
    callbacks: tuple[
        Callable[[LoadOptions, LoadState, pd.DataFrame], tuple[pd.DataFrame, list[dict[str, Any]]]],
        Callable[[LoadOptions, LoadState, pd.DataFrame], dict[str, str]],
    ],
) -> None:
    """Publish the same complete owned payload that the actual encoder certified."""
    if not client_validation_pending(options, df):
        return
    converter, incoming_types = callbacks
    if "native_assignment_configuration_digest" in options.destination_context:
        freeze_assignment_identity(options)
    frame, resolutions = client_input_dataframe(options, state, df, converter)
    if options.table_schema is None and state.target_column_types is None:
        schema = {
            str(name): cast("str", resolution["destination_type"])
            for name, resolution in zip(frame.columns, resolutions)
            if isinstance(resolution.get("destination_type"), str)
        }
        if len(schema) != len(frame.columns):
            schema = incoming_types(options, state, df)
        options.destination_context.setdefault("client_inferred_schema", schema)
    owned = dataframe_from_columns(
        [
            [decode_value(encode_value(value)) for value in frame.iloc[:, index]]
            for index in range(len(frame.columns))
        ],
        [str(name) for name in frame.columns],
        index=frame.index,
        object_dtype=True,
    )
    incoming = incoming_types(options, state, owned)
    physical = (
        {name: state.target_column_types[name] for name in incoming}
        if state.target_column_types is not None
        and state.original_target_exists
        and options.write_mode in {"append", "truncate_insert", "upsert"}
        else incoming
    )
    certificate = cast(
        "ClientWriteValidation",
        validate_client_write_capabilities(
            options.connection_backend,
            connection,
            incoming,
            physical_schema=physical,
            batches=(owned,),
            writer_kind="dataframe",
            prospective_schema=not (
                state.original_target_exists
                and options.write_mode in {"append", "truncate_insert", "upsert"}
            ),
        ),
    )
    certificate.require_complete(len(owned))
    require_assignment_identity(options, state)
    state.client_validated_dataframe = owned
    state.client_type_resolutions = [
        {
            **resolution,
            "client_validation_basis": certificate.validation_basis,
            "client_validation_driver": certificate.driver,
            "client_certified_count": certificate.certified_input_count,
        }
        for resolution in resolutions
    ]
    # The existing retry scope retains this owned dataframe when it replaces a
    # disposable target state. No mutable caller values need to be reacquired.
    state.prepared_json_dataframe = owned


def client_input_dataframe(
    options: LoadOptions,
    state: LoadState,
    df: pd.DataFrame,
    converter: Callable[
        [LoadOptions, LoadState, pd.DataFrame], tuple[pd.DataFrame, list[dict[str, Any]]]
    ],
) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    if state.client_validated_dataframe is not None:
        require_assignment_identity(options, state)
        return state.client_validated_dataframe, state.client_type_resolutions
    return converter(options, state, df)
