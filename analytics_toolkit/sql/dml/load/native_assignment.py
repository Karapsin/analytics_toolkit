"""Own complete native assignment payloads before publishing unprofiled GP types."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any, cast

from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.gp.native_assignment import (
    NativeAssignmentError,
    assignment_parameter_types,
    certify_assignment_checkpoint,
    native_assignment_errors,
    requires_keyed_append_assignment,
)
from analytics_toolkit.sql.backends.native_values import requires_native_assignment
from analytics_toolkit.sql.connection.config import get_connection_config
from analytics_toolkit.sql.core.replay_spool import FrozenInputIdentity
from analytics_toolkit.sql.dml.table.table_validation import effective_upsert_key_columns
from analytics_toolkit.sql.dml.transfer.runtime.retry import (
    is_non_retryable_sql_error,
    run_with_retry,
)
from analytics_toolkit.sql.execution.backend_options import current_backend_operation

from .load_sql_table import insert_table_batch
from .stage import create_stage_table
from .type_validation import (
    _validated_dataframe_profiles,
    convert_dataframe_schema,
    validate_dataframe_columns,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    import pandas as pd

    from analytics_toolkit.sql.backends.gp.native_assignment import NativeAssignmentCertificate
    from analytics_toolkit.sql.backends.value_profiles import ColumnProfile
    from analytics_toolkit.sql.connection.errors import SqlOperationContext
    from analytics_toolkit.sql.execution.plans import SqlOperationResult

    from .models import LoadOptions, LoadState


def run_assignment_operation(
    options: LoadOptions,
    operation: Callable[[int], int | SqlOperationResult],
    context_factory: Callable[[int], SqlOperationContext],
    retained_state: Callable[[], LoadState | None],
    callbacks: tuple[Callable[..., int | SqlOperationResult], Callable[..., None]],
) -> int | SqlOperationResult:
    """Own the retry guard and release its final checkpoint within one operation."""
    runner, cleanup = callbacks
    try:
        install_assignment_configuration_guard(options)
        return runner(
            operation_name=(
                f"loading DataFrame into {options.connection_key}.{options.destination_table}"
            ),
            retry_cnt=options.retry_cnt,
            timeout_increment=options.timeout_increment,
            operation=operation,
            context_factory=context_factory,
        )
    finally:
        try:
            state = retained_state()
            if state is not None and not state.upsert_checkpoint.prepared:
                cleanup(options, state, drop_created_target=True)
        finally:
            remove_assignment_configuration_guard(options)


def _configuration_digest(values: list[Any]) -> str:
    from analytics_toolkit.sql.dml.transfer.flow.adaptive_input import (  # noqa: PLC0415 -- reuse exact configuration hashing without a load/transfer import cycle.
        configuration_digest_from_configs,
    )

    return configuration_digest_from_configs(values)


def record_assignment_configuration(options: LoadOptions, config: Any) -> None:
    """Bind to the already resolved initial config without another alias lookup."""
    options.destination_context["native_assignment_configuration_digest"] = _configuration_digest(
        [config]
    )


def assignment_input_identity(options: LoadOptions) -> FrozenInputIdentity:
    from analytics_toolkit.sql.dml.transfer.flow.stage_identity import (  # noqa: PLC0415 -- canonical structured target identity.
        resolve_destination_identity,
    )

    return FrozenInputIdentity(
        source_key=options.connection_key,
        configuration_digest=_configuration_digest([get_connection_config(options.connection_key)]),
        namespace=(
            options.connection_backend,
            resolve_destination_identity(
                options.destination_table, options.connection_backend
            ).canonical,
            options.transfer_staging_schema or "",
            options.transfer_staging_username or "",
            options.destination_hash or "",
        ),
        query_digest=_configuration_digest(
            [
                options.key_columns,
                options.write_mode,
                options.staging_ddl_properties,
                options.regular_ddl_properties,
            ]
        ),
        destination_timezone=options.destination_context.get("destination_timezone"),
        timezone_policy_version=options.destination_context.get("timezone_policy_version"),
    )


def install_assignment_configuration_guard(options: LoadOptions) -> None:
    """Record actual opened configs; enforce the frozen scope once assignment starts."""
    operation = current_backend_operation()
    if operation is None:
        return
    cache = options.destination_context
    key = ("load_native_assignment", id(cache))
    cache["native_assignment_guard_key"] = key

    def guard(alias: str, resolved_config: Any = None) -> None:
        if alias.strip().lower() != options.connection_key.strip().lower():
            return
        if resolved_config is not None:
            actual = _configuration_digest([resolved_config])
            initial = cache.get("native_assignment_configuration_digest")
            if initial is not None and actual != initial:
                cache["native_assignment_opened_configuration_changed"] = True
        if "native_assignment_input_identity" in cache:
            require_assignment_identity(options)

    with operation.lock:
        operation.resources.setdefault("connection_configuration_guards", {})[key] = guard


def remove_assignment_configuration_guard(options: LoadOptions) -> None:
    key = options.destination_context.pop("native_assignment_guard_key", None)
    operation = current_backend_operation()
    if key is not None and operation is not None:
        with operation.lock:
            operation.resources.get("connection_configuration_guards", {}).pop(key, None)


def _identity_error(options: LoadOptions, message: str) -> NativeAssignmentError:
    resources = tuple(options.destination_context.get("native_assignment_owned_resources", ()))
    if resources:
        message += " Owned checkpoints remain at the original certified destination: " + ", ".join(
            resources
        )
    error = NativeAssignmentError(message)
    error.__dict__.update(
        {
            "analytics_toolkit_sql_acknowledged_resources": resources,
            "analytics_toolkit_sql_native_assignment_identity": options.destination_context.get(
                "native_assignment_input_identity"
            ),
        }
    )
    return error


def _own_assignment_resource(options: LoadOptions, state: LoadState, resource: str) -> None:
    state.owned_stage_resources.append(
        (resource, state.stage_candidates[-1], options.staging_ch_policy)
    )
    options.destination_context.setdefault("native_assignment_owned_resources", []).append(resource)


def require_assignment_identity(options: LoadOptions, state: LoadState | None = None) -> None:
    expected = options.destination_context.get("native_assignment_input_identity")
    certificates = (
        (state.native_assignment_source_checkpoint, state.native_assignment_checkpoint)
        if state is not None
        else ()
    )
    if any(
        cert is not None and (expected is None or cert.input_identity != expected)
        for cert in certificates
    ):
        message = "Native assignment checkpoint is not bound to the frozen destination identity."
        raise _identity_error(options, message)
    if expected is None:
        return
    if (
        options.destination_context.get("native_assignment_opened_configuration_changed")
        or assignment_input_identity(options) != expected
    ):
        message = (
            "Destination configuration or namespace differs from the certified "
            "native assignment checkpoint."
        )
        raise _identity_error(options, message)


def freeze_assignment_identity(options: LoadOptions) -> FrozenInputIdentity:
    cache = options.destination_context
    current = assignment_input_identity(options)
    initial = cache.get("native_assignment_configuration_digest", current.configuration_digest)
    if current.configuration_digest != initial or cache.get(
        "native_assignment_opened_configuration_changed"
    ):
        message = (
            "Destination configuration differs from the initially selected native assignment scope."
        )
        raise _identity_error(options, message)
    previous = cache.setdefault("native_assignment_input_identity", current)
    require_assignment_identity(options)
    return cast("FrozenInputIdentity", previous)


def can_cleanup_assignment(options: LoadOptions) -> bool:
    """Never drop an old-host checkpoint through a retargeted connection alias."""
    try:
        require_assignment_identity(options)
    except NativeAssignmentError:
        return False
    return True


def assignment_required(schema: Mapping[str, str] | None, backend: str) -> bool:
    """A local unknown is deferred only to an actual owned assignment checkpoint."""
    return any(requires_native_assignment(native, backend) for native in (schema or {}).values())


def _keyed_append_assignment_required(
    options: LoadOptions, schema: Mapping[str, str] | None = None
) -> bool:
    """Consult backend policy for equality after the selected native assignments."""
    return requires_keyed_append_assignment(
        options.connection_backend,
        options.write_mode,
        options.key_columns,
        options.table_schema,
        schema,
    )


def assignment_validation_pending(options: LoadOptions) -> bool:
    """Offline plans cannot inspect native types of an existing destination."""
    if _keyed_append_assignment_required(options):
        return True
    if options.table_schema is not None:
        return assignment_required(options.table_schema, options.connection_backend)
    return options.write_mode in {
        "append",
        "truncate_insert",
        "upsert",
    } and requires_native_assignment("", options.connection_backend)


def local_validation_view(
    df: pd.DataFrame, schema: Mapping[str, str], backend: str
) -> tuple[pd.DataFrame, dict[str, str]]:
    """Keep name coverage strict while delegating only unknown native domains."""
    names = validate_dataframe_columns(df, schema)
    known = {
        name: schema[name]
        for name in names
        if not requires_native_assignment(schema[name], backend)
    }
    return df.loc[:, list(known)], known


def validated_local_profiles(
    df: pd.DataFrame,
    schema: Mapping[str, str],
    backend: str,
    *,
    allow_timestamp_precision_loss: bool = True,
    destination_timezone: str | None = None,
) -> dict[str, ColumnProfile]:
    frame, known = local_validation_view(df, schema, backend)
    return _validated_dataframe_profiles(
        frame,
        known,
        backend,
        allow_timestamp_precision_loss=allow_timestamp_precision_loss,
        destination_timezone=destination_timezone,
    )


def convert_local_dataframe(
    df: pd.DataFrame,
    schema: Mapping[str, str],
    backend: str,
    *,
    destination_timezone: str | None = None,
    converter: Callable[..., tuple[pd.DataFrame, list[dict[str, Any]]]] = convert_dataframe_schema,
) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    """Preserve opaque native inputs for the driver; convert only known columns."""
    frame, known = local_validation_view(df, schema, backend)
    if len(known) == len(df.columns):
        return converter(df, schema, backend, destination_timezone=destination_timezone)
    converted, metadata = converter(
        frame, known, backend, destination_timezone=destination_timezone
    )
    result = df.copy(deep=False)
    for name in known:
        result[name] = converted[name]
    return result, metadata


def prepare_assignment_checkpoint(
    options: LoadOptions,
    state: LoadState,
    connection: Any,
    df: pd.DataFrame,
    schema: Mapping[str, str],
) -> None:
    """Certify explicit assignment, then actual destination assignment, before publication."""
    final_schema = {str(name): schema[str(name)] for name in df.columns}
    requested = {
        str(name): (options.table_schema or final_schema)[str(name)] for name in df.columns
    }
    source = state.native_assignment_source_checkpoint
    if source is not None:
        _require_checkpoint_schema(source, requested)
    if state.native_assignment_checkpoint is not None:
        _require_checkpoint_schema(state.native_assignment_checkpoint, final_schema)
        return
    keyed_assignment = _keyed_append_assignment_required(options, final_schema)
    if not (
        assignment_required(final_schema, options.connection_backend)
        or assignment_required(requested, options.connection_backend)
        or keyed_assignment
    ):
        return
    explicit_first = requested != final_schema and (
        assignment_required(requested, options.connection_backend) or keyed_assignment
    )
    if explicit_first:
        if source is None:
            source = _populate_checkpoint(options, state, connection, df, schema=requested)
            state.native_assignment_source_checkpoint = source
        _require_checkpoint_schema(source, requested)
    certificate = _populate_checkpoint(options, state, connection, df, schema=final_schema)
    state.native_assignment_checkpoint = certificate
    state.overlap_stage_table = certificate.stage_table


def _require_checkpoint_schema(certificate: Any, schema: Mapping[str, str]) -> None:
    if certificate.column_types != tuple(schema.items()):
        message = "Native assignment checkpoint schema differs from the destination authority."
        raise NativeAssignmentError(message)


def _populate_checkpoint(
    options: LoadOptions,
    state: LoadState,
    connection: Any,
    df: pd.DataFrame,
    *,
    schema: Mapping[str, str],
) -> NativeAssignmentCertificate:
    identity = freeze_assignment_identity(options)
    source = state.native_assignment_source_checkpoint
    adapter = get_backend_adapter(options.connection_backend)
    native_schema = {str(name): schema[str(name)] for name in df.columns}
    with native_assignment_errors("creation", non_retryable_predicate=is_non_retryable_sql_error):
        stage = create_stage_table(
            options.connection_backend,
            connection,
            options.destination_table,
            df,
            table_schema=native_schema,
            gp_distributed_by_key=options.gp_distributed_by_key,
            connection_key=options.connection_key,
            query_label=options.query_label,
            transfer_staging_schema=options.transfer_staging_schema,
            transfer_staging_username=options.transfer_staging_username,
            destination_hash=options.destination_hash,
            ddl_properties=options.staging_ddl_properties,
            on_stage_candidate=state.stage_candidates.append,
            on_resource_created=lambda resource: _own_assignment_resource(options, state, resource),
        )
    if not df.empty:
        with native_assignment_errors(
            "population", non_retryable_predicate=is_non_retryable_sql_error
        ):
            if source is not None:
                # Identical column order on owned stages makes this a native
                # assignment, with no explicit CAST or second Python adaptation.
                _insert_assigned_rows(
                    options, connection, stage, source.stage_table, list(native_schema)
                )
            else:
                insert_table_batch(
                    options.connection_backend,
                    {"connection": connection},
                    stage,
                    df,
                    retry_fn=run_with_retry,
                    retry_cnt=1,
                    timeout_increment=0,
                    target_column_types=assignment_parameter_types(
                        adapter,
                        connection,
                        stage,
                        options.table_schema or native_schema,
                        query_label=options.query_label,
                    ),
                    gp_insert_chunk_size=options.gp_insert_chunk_size,
                    query_label=options.query_label,
                    connection_key=options.connection_key,
                    safe_exception_logging=True,
                )
    keys = effective_upsert_key_columns(options.key_columns, options.upsert_partition_column)
    return certify_assignment_checkpoint(
        adapter,
        connection,
        stage,
        native_schema,
        expected_rows=len(df),
        key_columns=keys,
        input_identity=identity,
        query_label=options.query_label,
    )


def _insert_assigned_rows(
    options: LoadOptions,
    connection: Any,
    target: str,
    source: str,
    columns: list[str],
) -> None:
    adapter = get_backend_adapter(options.connection_backend)
    adapter.execute_command(
        connection,
        adapter.build_insert_from_stage_sql(
            target,
            source,
            columns=columns,
            column_types=None,
            query_label=options.query_label,
        ),
    )


def insert_checkpoint_rows(options: LoadOptions, state: LoadState, connection: Any) -> None:
    """Assign already-certified native values using a named, cast-free projection."""
    require_assignment_identity(options, state)
    certificate = state.native_assignment_checkpoint
    if certificate is None:
        message = "Native assignment publication requires a complete checkpoint."
        raise RuntimeError(message)
    _insert_assigned_rows(
        options,
        connection,
        options.destination_table,
        certificate.stage_table,
        [name for name, _native in certificate.column_types],
    )


def prepare_load_profiles(
    options: LoadOptions,
    state: LoadState,
    connection: Any,
    df: pd.DataFrame,
    validators: tuple[
        Callable[..., dict[str, ColumnProfile]],
        Callable[..., tuple[pd.DataFrame, list[dict[str, Any]]]],
    ],
) -> tuple[str, ...]:
    """Certify local domains and defer only columns requiring actual native assignment."""
    require_assignment_identity(options, state)
    zone = options.destination_context.get("destination_timezone")
    profile_validator, frame_converter = validators
    explicit_unknown = {
        name
        for name, native in (options.table_schema or {}).items()
        if requires_native_assignment(native, options.connection_backend)
    }
    null_columns: tuple[str, ...] = ()
    if state.target_column_types is not None:
        validate_dataframe_columns(df, state.target_column_types)
        frame = (
            convert_local_dataframe(
                df,
                options.table_schema,
                options.connection_backend,
                destination_timezone=zone,
                converter=frame_converter,
            )[0]
            if options.table_schema is not None
            else df
        )
        names = [str(name) for name in frame.columns if str(name) not in explicit_unknown]
        local_df, local_schema = local_validation_view(
            frame.loc[:, names],
            {name: state.target_column_types[name] for name in names},
            options.connection_backend,
        )
        profiles = profile_validator(
            local_df,
            local_schema,
            options.connection_backend,
            allow_timestamp_precision_loss=False,
            destination_timezone=zone,
        )
        null_columns = tuple(name for name, profile in profiles.items() if profile.null_count)
    if options.table_schema is not None:
        validated_local_profiles(
            df, options.table_schema, options.connection_backend, destination_timezone=zone
        )
    authority = (
        state.target_column_types
        if state.original_target_exists
        and options.write_mode in {"append", "truncate_insert", "upsert"}
        else options.table_schema
    )
    if authority is not None and (
        assignment_required(
            {str(name): authority[str(name)] for name in df.columns}, options.connection_backend
        )
        or explicit_unknown
        or _keyed_append_assignment_required(options, authority)
    ):
        payload, _ = convert_local_dataframe(
            df,
            options.table_schema or authority,
            options.connection_backend,
            destination_timezone=zone,
        )
        prepare_assignment_checkpoint(options, state, connection, payload, authority)
        null_columns = cast(
            "NativeAssignmentCertificate", state.native_assignment_checkpoint
        ).null_columns
    return null_columns


def load_input_dataframe(
    options: LoadOptions,
    state: LoadState,
    df: pd.DataFrame,
    prepare: Callable[[], Any],
    converter: Callable[
        [LoadOptions, LoadState, pd.DataFrame], tuple[pd.DataFrame, list[dict[str, Any]]]
    ],
) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    """Replay certified stages without converting opaque caller objects again."""
    require_assignment_identity(options, state)
    if has_assignment_checkpoint(state) and not state.native_assignment_target_prepared:
        prepare()
    if state.native_assignment_checkpoint is not None:
        return (
            state.prepared_json_dataframe if state.prepared_json_dataframe is not None else df
        ), []
    return converter(options, state, df)


def has_assignment_checkpoint(state: LoadState) -> bool:
    return (
        state.native_assignment_checkpoint is not None
        or state.native_assignment_source_checkpoint is not None
    )


def publish_assignment_checkpoint(
    options: LoadOptions, state: LoadState, df: pd.DataFrame, run_action: Any, finalize: Any
) -> int:
    require_assignment_identity(options, state)
    run_action(
        options,
        "finalize_target",
        lambda ref: finalize(
            options=checkpoint_options(options, state),
            state=state,
            connection=ref["connection"],
            df=df,
        ),
    )
    return cast("NativeAssignmentCertificate", state.native_assignment_checkpoint).row_count


def checkpoint_options(options: LoadOptions, state: LoadState) -> LoadOptions:
    """Publish stored native values without repeating client adaptation or stage casts."""
    certificate = state.native_assignment_checkpoint
    if certificate is None:
        message = "Native assignment publication requires a complete checkpoint."
        raise RuntimeError(message)
    return replace(options, table_schema=dict(certificate.column_types), use_parquet_staging=False)
