"""Resolve uncertain transport against one complete, certified input generation.

The ordinary metadata-proven streaming path does not pass through a spool. An
adaptive transfer has an intentional all-key barrier: all selected values are
profiled before any destination table is created or modified.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field, fields, is_dataclass, replace
from typing import TYPE_CHECKING, Any, NoReturn, cast

from analytics_toolkit.general import time_print
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.conversion_profiles import validate_converted_rows
from analytics_toolkit.sql.backends.models import SourceColumn
from analytics_toolkit.sql.backends.stage_ownership import single_resource_policy
from analytics_toolkit.sql.connection.config import get_connection_config
from analytics_toolkit.sql.connection.get_sql_connection import get_sql_connection
from analytics_toolkit.sql.core.replay_spool import FrozenInputIdentity, ReplaySpool
from analytics_toolkit.sql.core.type_resolution import (
    ColumnProfile,
    ResolvedColumn,
    declared_domain_fits,
    is_timestamp_type,
    resolve_column_type,
    timestamp_scale,
)
from analytics_toolkit.sql.ddl.schema import validate_table_schema_columns
from analytics_toolkit.sql.dml.destination_context import freeze_destination_timezone
from analytics_toolkit.sql.dml.load.stage import cleanup_stage_table
from analytics_toolkit.sql.dml.table.table_validation import validate_complete_upsert_columns
from analytics_toolkit.sql.dml.transfer.flow import row_counts
from analytics_toolkit.sql.dml.transfer.flow.conversion_validation import (
    resolve_final_conversions,
    validate_final_conversions,
)
from analytics_toolkit.sql.dml.transfer.flow.row_counts import (
    disable_query_limit_for_transfer_reads,
)
from analytics_toolkit.sql.dml.transfer.io.source import iter_source_batches, normalize_source_batch
from analytics_toolkit.sql.dml.transfer.runtime.models import (
    RowBatch,
    TransferConnectionRefs,
    TransferOptions,
    TransferSliceRowCount,
    TransferStageState,
)
from analytics_toolkit.sql.dml.transfer.runtime.retry import (
    close_connection_ref,
    run_with_fresh_connection,
)
from analytics_toolkit.sql.dml.transfer.schema import (
    get_existing_target_insert_types,
    map_source_schema_to_target,
)
from analytics_toolkit.sql.execution.backend_options import current_backend_operation
from analytics_toolkit.sql.execution.cancellation import raise_if_cancelled


class AdaptiveTransferCompleteError(Exception):
    """Internal control flow that still executes normal attempt cleanup."""

    def __init__(self, rows: int) -> None:
        self.rows = rows
        super().__init__()


if TYPE_CHECKING:
    from collections.abc import Iterator


@dataclass
class AdaptiveTransferContext:
    read_retry_cnt: int
    insert_retry_cnt: int
    reader: Any = None
    existing_types_reader: Any = None
    open_connection: Any = None
    target_connection_runner: Any = None
    target_host_connection_runner: Any = None
    force_freeze: bool = False


@dataclass(frozen=True)
class AdaptiveInputReady:
    context: AdaptiveTransferContext


@dataclass
class _ResolutionSchema:
    columns: list[SourceColumn]
    target_types: dict[str, str]
    authoritative: bool


@dataclass
class _SelectedInput:
    queries: list[str]
    snapshot_table: str | None = None
    expected_rows: int | None = None
    slice_counts: dict[int, int] = field(default_factory=dict)


@dataclass
class _InputScan:
    selected: _SelectedInput
    spool: ReplaySpool
    profiles: list[ColumnProfile]
    configuration_digest: str
    observed_scope: dict[Any, str] = field(default_factory=dict)


class AdaptiveInputValidationError(ValueError):
    """An input generation could not be certified without changing its meaning."""

    analytics_toolkit_sql_retry_safe = False


def _reject(message: str) -> NoReturn:
    raise AdaptiveInputValidationError(message)


def complete_adaptive_attempt(rows: int | None) -> None:
    """Leave the ordinary path while preserving its shared cleanup handlers."""
    if rows is not None:
        raise AdaptiveTransferCompleteError(rows)


def _target_types(
    options: TransferOptions,
    refs: TransferConnectionRefs,
    state: TransferStageState,
    source_schema: list[SourceColumn],
    context: AdaptiveTransferContext,
) -> tuple[dict[str, str], bool]:
    names = [column.name for column in source_schema]
    if options.table_schema is not None:
        types = validate_table_schema_columns(options.table_schema, names)
        authoritative = True
    else:
        types = map_source_schema_to_target(
            source_schema,
            options.to_db_backend,
            source_backend=options.from_db_backend,
        )
        authoritative = False
    if not state.target_exists or (
        options.write_mode == "replace" and options.table_schema is not None
    ):
        # Explicit replacement DDL is a caller-requested schema change. Other
        # modes must keep validating against the unchanged live target schema.
        return types, authoritative

    def inspect(target_ref: dict[str, Any]) -> dict[str, str]:
        if options.write_mode == "upsert":
            writable = get_backend_adapter(options.to_db_backend).get_writable_table_column_types(
                target_ref["connection"],
                options.target_table,
                connection_key=options.to_db_key,
            )
            validate_complete_upsert_columns(names, writable)
        raw_existing = (context.existing_types_reader or get_existing_target_insert_types)(
            options.to_db_backend,
            target_ref["connection"],
            options.target_table,
            types,
            connection_key=options.to_db_key,
        )
        # The explicit conversion remains authoritative when supplied; inserts
        # still use the real target types and must validate against both below.
        existing = {str(name): str(native_type) for name, native_type in raw_existing.items()}
        state.insert_column_types = existing
        return existing

    existing = (
        inspect(refs.target)
        if refs.target.get("connection") is not None
        else run_with_fresh_connection(
            options.to_db_key,
            "adaptive_target_metadata",
            inspect,
            open_connection=context.open_connection or get_sql_connection,
        )
    )
    return (types if options.table_schema is not None else existing), True


def _configuration_payload(value: Any) -> Any:
    # Walk immutable mappings directly; dataclasses.asdict deep-copies leaves
    # and cannot handle MappingProxyType used by resolved DDL policies.
    if is_dataclass(value) and not isinstance(value, type):
        return {
            item.name: _configuration_payload(getattr(value, item.name)) for item in fields(value)
        }
    if isinstance(value, Mapping):
        return {str(key): _configuration_payload(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_configuration_payload(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_configuration_payload(item) for item in value), key=str)
    return value


def configuration_digest_from_configs(configs: list[Any]) -> str:
    # Include secret fields in the hash; dataclass repr intentionally omits them.
    # Neither the source objects nor this private digest are emitted in logs.
    payload = _configuration_payload(configs)
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def configuration_identity_from_configs(configs: list[Any]) -> dict[str, Any]:
    return {
        "configuration_digest": configuration_digest_from_configs(configs),
        "configuration_digests": {
            config.connection_key.lower(): configuration_digest_from_configs([config])
            for config in configs
        },
    }


def _configuration_digest(options: TransferOptions) -> str:
    return configuration_digest_from_configs(
        [
            get_connection_config(options.from_db_key),
            get_connection_config(options.to_db_key),
        ]
    )


def require_frozen_configuration(options: TransferOptions) -> None:
    expected = options.input_resolution.get("configuration_digest")
    if expected is not None and expected != _configuration_digest(options):
        _reject("Connection configuration differs from certified transfer input.")


def install_configuration_guard(options: TransferOptions, *, configured_only: bool = False) -> None:
    cached = options.input_resolution
    expected = cached.get("configuration_digest")
    if configured_only and expected is None:
        return
    if expected is None or "configuration_digests" not in cached:
        configs = [
            get_connection_config(options.from_db_key),
            get_connection_config(options.to_db_key),
        ]
        identity = configuration_identity_from_configs(configs)
        if expected is not None and identity["configuration_digest"] != expected:
            _reject("Connection configuration differs from certified transfer input.")
        cached.update(identity)
    require_frozen_configuration(options)
    operation = current_backend_operation()
    if operation is None:
        return
    key = ("transfer_configuration", options.transfer_id or id(cached))
    aliases = {options.from_db_key.lower(), options.to_db_key.lower()}

    def guard(alias: str, resolved_config: Any = None) -> None:
        normalized = alias.strip().lower()
        if normalized not in aliases:
            return
        if resolved_config is not None:
            actual = configuration_digest_from_configs([resolved_config])
            if actual != cached["configuration_digests"][normalized]:
                _reject("Connection configuration differs from certified transfer input.")
        require_frozen_configuration(options)

    with operation.lock:
        operation.resources.setdefault("connection_configuration_guards", {})[key] = guard
    cached["configuration_guard_key"] = key


def remove_configuration_guard(options: TransferOptions) -> None:
    key = options.input_resolution.pop("configuration_guard_key", None)
    operation = current_backend_operation()
    if operation is not None and key is not None:
        with operation.lock:
            operation.resources.get("connection_configuration_guards", {}).pop(key, None)


def _availability_scope(options: TransferOptions) -> tuple[str, ...]:
    operation = current_backend_operation()
    if operation is None:
        return ()
    return tuple(
        sorted(
            json.dumps(status, sort_keys=True, default=str)
            for status in operation.statuses.values()
            if status.get("alias", "").lower() == options.from_db_key.lower()
        )
    )


def _check_generation_scope(options: TransferOptions, observed: dict[Any, str]) -> None:
    operation = current_backend_operation()
    if operation is None:
        return
    for key, status in operation.statuses.items():
        if status.get("alias", "").lower() != options.from_db_key.lower():
            continue
        signature = json.dumps(status, sort_keys=True, default=str)
        if key in observed and observed[key] != signature:
            _reject("Selected source availability scope changed during input profiling.")
        observed[key] = signature


def _selected_input(
    options: TransferOptions,
    refs: TransferConnectionRefs,
    state: TransferStageState,
    names: list[str],
    configuration_digest: str,
) -> _SelectedInput:
    if options.source_transfer_staging_schema is None:
        return _SelectedInput(
            [item.source_sql for item in options.transfer_slices]
            if options.transfer_slices is not None
            else [options.source_sql],
        )
    # Kept local to break the staged-attempt/adaptive orchestration cycle.
    from analytics_toolkit.sql.dml.transfer.flow.staged_attempt import (  # noqa: PLC0415
        _materialize_snapshot,
    )

    native = options.input_resolution.get("native_snapshot")
    if native is None:
        snapshot_table, counts = _materialize_snapshot(options, refs.source, state)
        expected_rows = sum(counts.values())
        options.input_resolution["native_snapshot"] = (
            snapshot_table,
            expected_rows,
            configuration_digest,
            counts,
        )
    else:
        snapshot_table, expected_rows, snapshot_digest, counts = native
        if snapshot_digest != configuration_digest:
            _reject("Connection configuration differs from native source checkpoint.")
    adapter = get_backend_adapter(options.from_db_backend)
    projection = ", ".join(adapter.quote_identifier(name) for name in names)
    # Columns are adapter-quoted and the table is allocated by the stage owner.
    query = f"SELECT {projection} FROM {snapshot_table}"  # noqa: S608
    return _SelectedInput([query], snapshot_table, expected_rows, counts)


def _require_source_snapshot(options: TransferOptions) -> None:
    # Use the shared runtime guard, including its source-coverage observations.
    row_counts.require_partial_source_snapshot(options)


def _profile_batch(
    options: TransferOptions,
    state: TransferStageState,
    scan: _InputScan,
    source_batch: RowBatch,
    source_connection: Any = None,
) -> None:
    raise_if_cancelled()
    _check_generation_scope(options, scan.observed_scope)
    if list(source_batch.columns) != list(scan.spool.columns):
        _reject("Source columns changed while freezing selected input.")
    batch = normalize_source_batch(
        options, source_connection, source_batch, state.source_column_types or {}
    )
    for row in batch.rows:
        for profile, value in zip(scan.profiles, row):
            profile.add(value)
    scan.spool.write_rows(batch.rows)
    _require_source_snapshot(options)


def _scan_input(
    options: TransferOptions,
    refs: TransferConnectionRefs,
    state: TransferStageState,
    scan: _InputScan,
    context: AdaptiveTransferContext,
) -> None:
    reader = context.reader or iter_source_batches
    for query_index, query in enumerate(scan.selected.queries):
        rows_before_query = scan.spool.row_count
        batches = reader(
            options.from_db_key,
            options.from_db_backend,
            refs.source,
            query,
            options.batch_size,
            retry_cnt=context.read_retry_cnt,
            timeout_increment=options.timeout_increment,
            query_label=options.query_label,
            disable_ch_query_limit=disable_query_limit_for_transfer_reads(options.from_db_backend),
        )
        try:
            for source_batch in batches:
                _profile_batch(options, state, scan, source_batch, refs.source["connection"])
        finally:
            close = getattr(batches, "close", None)
            if callable(close):
                close_connection_ref(
                    {"connection": batches}, options.from_db_key, "profiling_stream"
                )
        if scan.selected.snapshot_table is None:
            slice_id = options.transfer_slices[query_index].index if options.transfer_slices else 0
            scan.selected.slice_counts[slice_id] = scan.spool.row_count - rows_before_query


def _resolve_scan(
    options: TransferOptions,
    schema: _ResolutionSchema,
    scan: _InputScan,
) -> list[ResolvedColumn]:
    expected = scan.selected.expected_rows
    if expected is not None and scan.spool.row_count != expected:
        _reject("Frozen input count differs from its materialized source checkpoint.")
    if scan.configuration_digest != _configuration_digest(options):
        _reject("Connection configuration changed while freezing selected input.")
    if options.table_schema is None and schema.authoritative:
        for column, profile in zip(schema.columns, scan.profiles):
            target_type = schema.target_types[column.name]
            if is_timestamp_type(target_type) and profile.temporal_scale > timestamp_scale(
                target_type
            ):
                _reject("Selected input exceeds authoritative timestamp precision.")
    return [
        resolve_column_type(
            column,
            options.to_db_backend,
            profile,
            source_backend=options.from_db_backend,
            authoritative_type=schema.target_types[column.name] if schema.authoritative else None,
            allow_timestamp_precision_loss=options.table_schema is not None,
            destination_timezone=options.input_resolution.get("destination_timezone"),
        )
        for column, profile in zip(schema.columns, scan.profiles)
    ]


def _validate_resolved_input(
    options: TransferOptions,
    state: TransferStageState,
    schema: _ResolutionSchema,
    spool: ReplaySpool,
    resolutions: list[ResolvedColumn],
) -> None:
    try:
        profiles = validate_converted_rows(
            spool.iter_rows(),
            resolutions,
            backend=options.to_db_backend,
            destination_timezone=options.input_resolution.get("destination_timezone"),
        )
    except (TypeError, ValueError, OverflowError):
        _reject("Selected input cannot be converted to the resolved destination schema.")
    targets = resolve_final_conversions(
        schema.columns,
        resolutions,
        profiles,
        backend=options.to_db_backend,
        existing_types=state.insert_column_types,
        destination_timezone=options.input_resolution.get("destination_timezone"),
    )
    if targets is not None:
        try:
            validate_final_conversions(spool, resolutions, targets)
        except (TypeError, ValueError, OverflowError):
            _reject("Selected input cannot be converted to the resolved destination schema.")


def _freeze(
    options: TransferOptions,
    refs: TransferConnectionRefs,
    state: TransferStageState,
    schema: _ResolutionSchema,
    context: AdaptiveTransferContext,
) -> tuple[ReplaySpool, list[ResolvedColumn]]:
    names = [column.name for column in schema.columns]
    install_configuration_guard(options)
    digest = _configuration_digest(options)
    identity = FrozenInputIdentity(
        source_key=options.from_db_key,
        configuration_digest=digest,
        namespace=(options.from_db_backend, options.to_db_backend, options.target_table),
        destination_timezone=options.input_resolution.get("destination_timezone"),
        timezone_policy_version=options.input_resolution.get("timezone_policy_version"),
        availability_scope=_availability_scope(options),
        query_digest=hashlib.sha256(
            json.dumps(
                [item.source_sql for item in options.transfer_slices]
                if options.transfer_slices is not None
                else [options.source_sql],
            ).encode()
        ).hexdigest(),
    )
    spool = ReplaySpool(names, identity=identity)
    selected = _SelectedInput([])
    try:
        _require_source_snapshot(options)
        selected = _selected_input(options, refs, state, names, digest)
        scan = _InputScan(
            selected,
            spool,
            [
                ColumnProfile(
                    native_type=column.native_type,
                    source_backend=options.from_db_backend,
                    destination_timezone=options.input_resolution.get("destination_timezone"),
                    temporal_type=schema.target_types[column.name]
                    if schema.authoritative
                    else None,
                )
                for column in schema.columns
            ],
            digest,
        )
        _scan_input(options, refs, state, scan, context)
        resolutions = _resolve_scan(options, schema, scan)
        _check_generation_scope(options, scan.observed_scope)
        spool.identity = replace(
            identity,
            availability_scope=_availability_scope(options),
            source_timezone=options.input_resolution.get("source_timezone"),
        )
        spool.certify()
        _validate_resolved_input(options, state, schema, spool, resolutions)
        labels = {item.index: item.label for item in options.transfer_slices or []}
        options.input_resolution["slice_counts"] = [
            TransferSliceRowCount(index, labels.get(index), count, count)
            for index, count in sorted(selected.slice_counts.items())
        ]
    except BaseException as exc:
        if isinstance(exc, (TypeError, ValueError, OverflowError)):
            exc.__dict__["analytics_toolkit_sql_retry_safe"] = False
        close_connection_ref({"connection": spool}, options.from_db_key, "incomplete_input")
        if selected.snapshot_table is not None:
            time_print(
                f"Retained source checkpoint {selected.snapshot_table}: "
                "adaptive input was not certified.",
                level="warning",
            )
        raise
    return spool, resolutions


def resolve_adaptive_input(
    options: TransferOptions,
    refs: TransferConnectionRefs,
    state: TransferStageState,
    source_schema: list[SourceColumn],
    context: AdaptiveTransferContext,
) -> bool:
    """Certify adaptive input, or retain the metadata-proven streaming path."""
    if not source_schema:
        return False
    source_schema = [
        SourceColumn(
            column.name,
            column.native_type,
            getattr(column, "precision", None),
            getattr(column, "scale", None),
        )
        for column in source_schema
    ]
    target_types, authoritative = _target_types(
        options,
        refs,
        state,
        source_schema,
        context,
    )
    cached = options.input_resolution

    def target_zone(ref: dict[str, Any]) -> str | None:
        return freeze_destination_timezone(
            cached, options.to_db_backend, ref["connection"], target_types
        )

    if refs.target.get("connection") is not None:
        target_zone(refs.target)
    else:
        run_target_operation(
            options,
            "resolve_write_context",
            target_zone,
            open_connection=context.open_connection or get_sql_connection,
        )
    contextual_profile = options.from_db_backend != "ch" and get_backend_adapter(
        options.to_db_backend
    ).requires_destination_timezone(target_types)
    needs_profile = (context.force_freeze or contextual_profile) or any(
        not declared_domain_fits(
            column.native_type,
            target_types[column.name],
            options.to_db_backend,
            source_backend=options.from_db_backend,
        )
        or (
            state.insert_column_types is not None
            and not declared_domain_fits(
                target_types[column.name],
                state.insert_column_types[column.name],
                options.to_db_backend,
                source_backend=options.to_db_backend,
            )
        )
        for column in source_schema
    )
    if not needs_profile and "spool" not in cached:
        state.stage_column_types = target_types
        state.input_types_resolved = authoritative
        return False
    cached["used"] = True
    if "spool" not in cached:
        spool, resolutions = _freeze(
            options,
            refs,
            state,
            _ResolutionSchema(source_schema, target_types, authoritative),
            context,
        )
        cached.update(
            spool=spool,
            resolutions=resolutions,
            source_schema=source_schema,
            input_count=spool.row_count,
        )
    else:
        spool, resolutions = cached["spool"], cached["resolutions"]
        if spool.identity.configuration_digest != _configuration_digest(options):
            _reject("Connection configuration differs from certified transfer input.")
        if authoritative and any(
            resolution.target_type != target_types[column.name]
            for column, resolution in zip(cached["source_schema"], resolutions)
        ):
            _reject("Destination schema differs from certified transfer input.")
    native = cached.get("native_snapshot")
    if native is not None:
        # Install the certified replacement first, so a failed source cleanup
        # retry reuses these exact rows rather than rerunning a mutable query.
        cleanup_stage_table(
            options.from_db_backend,
            refs.source["connection"],
            native[0],
            query_label=options.query_label,
            ch_creation_policy=single_resource_policy(options.source_staging_ch_policy),
        )
        cached.pop("native_snapshot")
    state.source_columns = list(spool.columns)
    state.source_column_types = {
        column.name: column.native_type for column in cached["source_schema"]
    }
    state.stage_column_types = dict(zip(spool.columns, (item.target_type for item in resolutions)))
    state.input_types_resolved = True
    state.expected_source_rows = spool.row_count
    state.current_expected_source_rows = spool.row_count
    state.slice_counts = list(cached.get("slice_counts", []))
    return True


def run_resolved_transfer(
    options: TransferOptions,
    refs: TransferConnectionRefs,
    state: TransferStageState,
    context: AdaptiveTransferContext,
) -> int:
    # One shared stage suffices once all selected keys have crossed the barrier.
    frozen_options = replace(options, transfer_slices=None, table_schema=state.stage_column_types)
    target_runner = context.target_connection_runner or (
        lambda role, operation: run_with_fresh_connection(
            options.to_db_key,
            role,
            operation,
            open_connection=context.open_connection or get_sql_connection,
        )
    )

    def runner(role: str, operation: Any) -> Any:
        require_frozen_configuration(options)

        def checked(ref: dict[str, Any]) -> Any:
            freeze_destination_timezone(
                options.input_resolution, options.to_db_backend, ref["connection"]
            )
            return operation(ref)

        return target_runner(role, checked)

    # Orchestration imports are local to avoid the public transfer import cycle.
    from analytics_toolkit.sql.dml.transfer.flow.attempt import load_stage_batches  # noqa: PLC0415
    from analytics_toolkit.sql.dml.transfer.flow.finalize import (  # noqa: PLC0415
        finalize_loaded_stage,
    )
    from analytics_toolkit.sql.dml.transfer.flow.row_counts import (  # noqa: PLC0415
        validate_loaded_stage_row_count,
    )
    from analytics_toolkit.sql.dml.transfer.flow.stage import (  # noqa: PLC0415
        ensure_transfer_target_table,
    )

    options.input_resolution["target_connection_runner"] = runner
    try:
        runner(
            "prepare_frozen_target",
            lambda target_ref: ensure_transfer_target_table(
                frozen_options,
                TransferConnectionRefs(source=refs.source, target=target_ref),
                state,
                state.source_columns,
            ),
        )
        total_rows = load_stage_batches(
            frozen_options,
            refs,
            state,
            context.read_retry_cnt,
            context.insert_retry_cnt,
        )
        validate_loaded_stage_row_count(
            options=frozen_options,
            connection_refs=refs,
            stage_state=state,
            total_rows=total_rows,
            open_connection=context.open_connection or get_sql_connection,
            target_connection_runner=runner,
        )
        finalize_loaded_stage(
            frozen_options,
            refs,
            state,
            total_rows,
            target_connection_runner=runner,
            target_host_connection_runner=context.target_host_connection_runner,
        )
        object.__setattr__(options, "row_count_result", frozen_options.row_count_result)
        object.__setattr__(options, "final_target_rows", frozen_options.final_target_rows)
        object.__setattr__(
            options,
            "transfer_concurrency",
            replace(options.transfer_concurrency, effective_read=1, effective_write=1),
        )
        return total_rows

    finally:
        options.input_resolution.pop("target_connection_runner", None)


def run_certified_transfer_attempt(
    options: TransferOptions,
    context: AdaptiveTransferContext,
) -> int:
    """Retry destination work without reopening or describing a mutable source."""
    from analytics_toolkit.sql.dml.transfer.flow.attempt import (  # noqa: PLC0415
        cleanup_stage,
        create_stage_state,
    )

    refs = TransferConnectionRefs()
    state: TransferStageState | None = None
    primary: BaseException | None = None
    try:
        state = cast(
            "TransferStageState",
            run_target_operation(
                options,
                "target_state",
                lambda target_ref: create_stage_state(
                    options,
                    TransferConnectionRefs(target=target_ref),
                ),
                open_connection=context.open_connection or get_sql_connection,
            ),
        )
        if options.input_resolution.get("native_snapshot") is not None:
            refs.source["connection"] = (context.open_connection or get_sql_connection)(
                options.from_db_key,
            )
        resolve_adaptive_input(
            options,
            refs,
            state,
            options.input_resolution["source_schema"],
            context,
        )
        return run_resolved_transfer(options, refs, state, context)
    except BaseException as exc:
        primary = exc
        raise
    finally:
        try:
            if state is not None:
                cleanup_stage(
                    options=options,
                    connection_refs=refs,
                    stage_state=state,
                    read_retry_cnt=context.read_retry_cnt,
                    drop_created_target=primary is not None,
                )
        except BaseException:
            if primary is None:
                raise
            time_print(
                "Stage cleanup also failed while retaining certified transfer input.",
                level="warning",
            )
        finally:
            close_connection_ref(refs.source, options.from_db_key, "source_checkpoint")


def maybe_run_adaptive_transfer(
    options: TransferOptions,
    refs: TransferConnectionRefs,
    state: TransferStageState,
    source_schema: list[SourceColumn],
    context: AdaptiveTransferContext,
) -> int | None:
    """Run the complete frozen path, or return None for ordinary streaming."""
    if not resolve_adaptive_input(options, refs, state, source_schema, context):
        return None
    return run_resolved_transfer(options, refs, state, context)


def run_target_operation(
    options: TransferOptions,
    role: str,
    operation: Any,
    *,
    open_connection: Any,
) -> Any:
    require_frozen_configuration(options)
    runner = options.input_resolution.get("target_connection_runner")
    if runner is not None:
        return runner(role, operation)
    return run_with_fresh_connection(
        options.to_db_key,
        role,
        lambda ref: _run_context_checked_target(options, ref, operation),
        open_connection=open_connection,
    )


def _run_context_checked_target(
    options: TransferOptions, ref: dict[str, Any], operation: Any
) -> Any:
    freeze_destination_timezone(options.input_resolution, options.to_db_backend, ref["connection"])
    return operation(ref)


def estimated_input_rows(
    options: TransferOptions, source_ref: dict[str, Any], estimate: Any
) -> int | None:
    if not options.progress or not options.estimate_total_rows:
        return None
    spool = options.input_resolution.get("spool")
    if spool is not None:
        return int(spool.row_count)
    result = estimate(options, source_ref["connection"])
    return None if result is None else int(result)


def resolved_source_batches(
    options: TransferOptions,
    source_batches: Any,
    *args: Any,
    **kwargs: Any,
) -> Iterator[RowBatch]:
    """Replay certified input with its frozen conversions, never rerun its SQL."""
    cached = options.input_resolution
    spool = cached.get("spool")
    if spool is None:
        yield from source_batches(*args, **kwargs)
        return
    resolutions = cached["resolutions"]
    size = kwargs.get("get_batch_size")
    for batch in spool.iter_batches(size() if size is not None else options.batch_size):
        raise_if_cancelled()
        require_frozen_configuration(options)
        yield RowBatch(
            columns=batch.columns,
            rows=[
                tuple(resolution.convert(value) for resolution, value in zip(resolutions, row))
                for row in batch.rows
            ],
        )


def close_frozen_input(options: TransferOptions) -> None:
    remove_configuration_guard(options)
    spool = options.input_resolution.pop("spool", None)
    if spool is not None:
        close_connection_ref({"connection": spool}, options.from_db_key, "replay_spool")
