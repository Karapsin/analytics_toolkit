"""Publish opaque GP native assignments from one certified owned checkpoint."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

import pandas as pd

from analytics_toolkit.general import time_print
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.creation_resources import require_created_resource
from analytics_toolkit.sql.backends.gp.native_assignment import (
    NativeAssignmentCertificate,
    assignment_parameter_types,
    certify_assignment_checkpoint,
    native_assignment_errors,
)
from analytics_toolkit.sql.backends.models import SourceColumn as _SourceColumn
from analytics_toolkit.sql.backends.native_values import requires_native_assignment
from analytics_toolkit.sql.backends.stage_ownership import drop_owned_resource
from analytics_toolkit.sql.dml.load.stage import create_stage_table
from analytics_toolkit.sql.dml.transfer.runtime.retry import (
    is_non_retryable_sql_error,
    run_with_fresh_connection,
)
from analytics_toolkit.sql.execution.backend_options import preserve_recovery_artifacts
from analytics_toolkit.sql.execution.cancellation import raise_if_cancelled

from .assignment_context import require_pending_assignment_context
from .conversion_validation import resolve_final_column_conversion
from .frozen_retry import frozen_input_retry
from .stage_ownership import stage_creation_callbacks

SourceColumn = _SourceColumn

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from analytics_toolkit.sql.backends.value_profiles import ColumnProfile
    from analytics_toolkit.sql.core.replay_spool import ReplaySpool
    from analytics_toolkit.sql.core.type_resolution import ResolvedColumn
    from analytics_toolkit.sql.ddl.creation_resources import CreatedTableResource
    from analytics_toolkit.sql.dml.transfer.runtime.models import (
        TransferOptions,
        TransferStageState,
    )


@dataclass(frozen=True)
class _RetainedAssignment:
    certificate: NativeAssignmentCertificate
    owned_resources: tuple[tuple[str, str], ...]
    destination_types: tuple[tuple[str, str], ...]
    final: bool
    scoped_resources: tuple[tuple[CreatedTableResource, str], ...] = ()


def final_key_assignment_required(
    options: TransferOptions,
    state: TransferStageState,
    stage_types: Mapping[str, str],
) -> bool:
    """Stored destination keys must certify any change of native key schema."""
    keys = getattr(options, "key_columns", None)
    if options.to_db_backend != "gp" or not keys or not state.insert_column_types:
        return False
    adapter = get_backend_adapter("gp")
    return any(
        adapter.canonical_native_type(stage_types[name])
        != adapter.canonical_native_type(state.insert_column_types[name])
        for name in keys
    )


def requires_native_input_profile(
    options: TransferOptions,
    state: TransferStageState,
    stage_types: Mapping[str, str],
    *,
    allow_pending: bool,
    requires_assignment: Callable[[str, str], bool],
) -> bool:
    if not allow_pending:
        return False
    return final_key_assignment_required(options, state, stage_types) or (
        options.from_db_key.lower() != options.to_db_key.lower()
        and any(
            requires_assignment(native, options.to_db_backend)
            for native in (*stage_types.values(), *(state.insert_column_types or {}).values())
        )
    )


def _known_final_conversions(
    options: TransferOptions,
    existing: Mapping[str, str] | None,
    columns: Sequence[SourceColumn],
    resolutions: Sequence[ResolvedColumn],
    profiles: Sequence[ColumnProfile],
) -> tuple[list[tuple[int, ResolvedColumn, ResolvedColumn]], list[str], bool]:
    known: list[tuple[int, ResolvedColumn, ResolvedColumn]] = []
    context_types: list[str] = []
    pending = False
    if existing is None:
        return known, context_types, pending
    for index, (column, stage, profile) in enumerate(zip(columns, resolutions, profiles)):
        target_type = existing[column.name]
        if stage.native_assignment_required or requires_native_assignment(target_type, "gp"):
            if stage.native_assignment_required and target_type != stage.target_type:
                context_types.append(target_type)
            continue
        target = resolve_final_column_conversion(
            column,
            stage,
            profile,
            backend="gp",
            target_type=target_type,
            destination_timezone=options.input_resolution.get("destination_timezone"),
            allow_pending_native_assignment=True,
        )
        if target.native_assignment_required:
            pending = True
            context_types.append(target_type)
        known.append((index, stage, target))
    return known, context_types, pending


def validate_pending_final_conversions(  # noqa: PLR0913 -- preserve both schema authorities.
    options: TransferOptions,
    state: TransferStageState,
    columns: Sequence[SourceColumn],
    resolutions: Sequence[ResolvedColumn],
    profiles: Sequence[ColumnProfile],
    spool: ReplaySpool,
) -> bool:
    """Defer only native chains whose actual typed intermediate is not local."""
    if options.to_db_backend != "gp":
        return False
    existing = state.insert_column_types
    key_assignment = final_key_assignment_required(
        options,
        state,
        {column.name: item.target_type for column, item in zip(columns, resolutions)},
    )
    pending = (
        key_assignment
        or any(item.native_assignment_required for item in resolutions)
        or (
            existing is not None
            and any(requires_native_assignment(existing[column.name], "gp") for column in columns)
        )
    )
    context_types = [item.target_type for item in resolutions if item.native_assignment_required]
    if key_assignment and existing is not None:
        context_types.extend(existing[name] for name in options.key_columns or [])
        context_types.extend(
            item.target_type
            for column, item in zip(columns, resolutions)
            if column.name in (options.key_columns or [])
        )
    known, final_context, final_pending = _known_final_conversions(
        options, existing, columns, resolutions, profiles
    )
    pending = pending or final_pending
    context_types.extend(final_context)
    if not pending:
        return False
    if existing is not None:
        for row in spool.iter_rows():
            raise_if_cancelled()
            for index, stage, target in known:
                target.convert(stage.convert(row[index]))
    options.input_resolution["native_assignment_pending"] = True
    options.input_resolution["native_assignment_context_types"] = tuple(
        dict.fromkeys(context_types)
    )
    return True


def native_assignment_pending(options: TransferOptions) -> bool:
    return bool(options.input_resolution.get("native_assignment_pending"))


def native_assignment_plan_options(
    options: TransferOptions, requires_assignment: Callable[[str, str], bool]
) -> dict[str, object]:
    """Describe unresolved native certification without scanning or staging input."""
    return {
        "native_assignment_validation_pending": options.to_db_backend == "gp"
        and (
            options.table_schema is None
            or bool(getattr(options, "key_columns", None))
            or any(requires_assignment(value, "gp") for value in options.table_schema.values())
        ),
        "native_assignment_validation": (
            "owned_typed_stage_before_live_publication_if_required"
            if options.to_db_backend == "gp"
            else None
        ),
    }


def native_insert_retry_arguments(options: TransferOptions, retry: Any) -> dict[str, Any]:
    retry = frozen_input_retry(options, retry)
    if not native_assignment_pending(options):
        return {"retry_fn": retry}

    def safe_retry(**kwargs: Any) -> Any:
        return retry(**kwargs, safe_exception_logging=True)

    return {"retry_fn": safe_retry, "safe_exception_logging": True}


def native_insert_parameter_types(
    options: TransferOptions, state: TransferStageState, connection: Any
) -> dict[str, str] | None:
    if not native_assignment_pending(options):
        return state.stage_column_types
    require_pending_assignment_context(options, connection)
    raw_types = state.stage_column_types
    if state.stage_table is None or raw_types is None:
        message = "Native assignment parameters require an acknowledged typed stage."
        raise ValueError(message)
    identity = (state.stage_table, tuple(raw_types.items()))
    cached = options.input_resolution.get("native_assignment_parameter_types")
    if cached is None or cached[0] != identity:
        types = assignment_parameter_types(
            get_backend_adapter("gp"),
            connection,
            state.stage_table,
            raw_types,
            query_label=options.query_label,
        )
        cached = (identity, types)
        options.input_resolution["native_assignment_parameter_types"] = cached
    return cast("dict[str, str]", cached[1])


def _final_types(state: TransferStageState) -> dict[str, str]:
    types = state.insert_column_types or state.stage_column_types
    if types is None:
        message = "Native assignment requires an authoritative destination schema."
        raise ValueError(message)
    return {name: types[name] for name in state.source_columns}


def restore_assignment_checkpoint(
    options: TransferOptions, state: TransferStageState, runner: Callable[..., Any]
) -> int | None:
    cached = cast(
        "_RetainedAssignment | None", options.input_resolution.get("native_assignment_checkpoint")
    )
    if cached is None:
        return None
    certificate = cached.certificate
    if tuple(_final_types(state).items()) != cached.destination_types:
        message = "Destination schema differs from the certified native assignment checkpoint."
        raise ValueError(message)
    spool = options.input_resolution["spool"]
    if certificate.input_identity != spool.identity:
        message = "Selected input differs from the certified native assignment checkpoint."
        raise ValueError(message)

    def certify(ref: dict[str, Any]) -> NativeAssignmentCertificate:
        if not cached.scoped_resources:
            message = "Native assignment replay has no acknowledged creation identity."
            raise ValueError(message)
        adapter = get_backend_adapter("gp")
        for resource, _stage in cached.scoped_resources:
            require_created_resource(adapter, ref["connection"], resource)
        return certify_assignment_checkpoint(
            adapter,
            ref["connection"],
            certificate.stage_table,
            dict(certificate.column_types),
            expected_rows=certificate.row_count,
            key_columns=certificate.key_columns,
            input_identity=certificate.input_identity,
            query_label=options.query_label,
            session_context=certificate.session_context,
        )

    runner(
        "validate_native_assignment_checkpoint",
        certify,
    )
    state.stage_table = certificate.stage_table
    state.stage_tables = None
    state.stage_table_created = True
    state.stage_column_types = dict(certificate.column_types)
    if cached.final:
        state.insert_column_types = dict(certificate.column_types)
    state.first_non_empty_batch = pd.DataFrame(columns=list(state.stage_column_types))
    state.owned_stage_resources.extend(cached.owned_resources)
    state.owned_stage_scoped_resources.extend(cached.scoped_resources)
    state.scoped_stage_ownership_requested = bool(cached.scoped_resources)
    state.stage_table_candidates.append(certificate.stage_table)
    state.stage_rows = certificate.row_count
    return certificate.row_count


def certify_loaded_assignment(
    options: TransferOptions,
    state: TransferStageState,
    total_rows: int,
    runner: Callable[..., Any],
) -> None:
    """Evaluate the real final INSERT SELECT into its own destination-typed table."""
    if not total_rows:
        return
    adapter = get_backend_adapter("gp")
    source_stage = state.stage_table
    stage_types = state.stage_column_types
    if source_stage is None or stage_types is None:
        message = "Native assignment checkpoint has no completely populated source stage."
        raise ValueError(message)
    final_types = _final_types(state)
    cached = cast(
        "_RetainedAssignment | None", options.input_resolution.get("native_assignment_checkpoint")
    )
    if cached is not None and cached.final:
        return
    source_resources = _stage_resources(state, source_stage)
    source_certificate = runner(
        "certify_native_assignment_input",
        lambda ref: certify_assignment_checkpoint(
            adapter,
            ref["connection"],
            source_stage,
            stage_types,
            expected_rows=total_rows,
            input_identity=options.input_resolution["spool"].identity,
            query_label=options.query_label,
            session_context=options.input_resolution.get("native_assignment_session_context"),
        ),
    )
    options.input_resolution["native_assignment_checkpoint"] = _RetainedAssignment(
        source_certificate,
        source_resources,
        tuple(final_types.items()),
        final=False,
        scoped_resources=_scoped_stage_resources(state, source_stage),
    )
    final_stage = source_stage
    if tuple(stage_types.items()) != tuple(final_types.items()):

        def create(ref: dict[str, Any]) -> str:
            with native_assignment_errors(
                "creation", non_retryable_predicate=is_non_retryable_sql_error
            ):
                return create_stage_table(
                    "gp",
                    ref["connection"],
                    options.target_table,
                    pd.DataFrame(columns=list(final_types)),
                    column_types=final_types,
                    gp_distributed_by_key=options.gp_distributed_by_key,
                    connection_key=options.to_db_key,
                    query_label=options.query_label,
                    transfer_staging_schema=options.transfer_staging_schema,
                    transfer_staging_username=options.transfer_staging_username,
                    destination_hash=options.destination_hash,
                    ddl_properties=options.staging_ddl_properties,
                    **stage_creation_callbacks(state, options.to_db_backend),
                )

        final_stage = runner("create_native_assignment_checkpoint", create)

        def populate(ref: dict[str, Any]) -> None:
            with native_assignment_errors(
                "population", non_retryable_predicate=is_non_retryable_sql_error
            ):
                adapter.execute_command(
                    ref["connection"],
                    adapter.build_insert_from_stage_sql(
                        final_stage,
                        source_stage,
                        columns=list(final_types),
                        column_types=final_types,
                        query_label=options.query_label,
                    ),
                )

        runner("populate_native_assignment_checkpoint", populate)
    certificate = runner(
        "certify_native_assignment_checkpoint",
        lambda ref: certify_assignment_checkpoint(
            adapter,
            ref["connection"],
            final_stage,
            final_types,
            expected_rows=total_rows,
            key_columns=options.key_columns,
            input_identity=options.input_resolution["spool"].identity,
            query_label=options.query_label,
            session_context=options.input_resolution.get("native_assignment_session_context"),
        ),
    )
    resources = _stage_resources(state, final_stage)
    options.input_resolution["native_assignment_checkpoint"] = _RetainedAssignment(
        certificate,
        resources,
        tuple(final_types.items()),
        final=True,
        scoped_resources=_scoped_stage_resources(state, final_stage),
    )
    options.input_resolution["native_assignment_certificate"] = certificate
    state.stage_table = final_stage
    state.stage_tables = None
    state.stage_column_types = final_types
    state.insert_column_types = final_types


def _stage_resources(state: TransferStageState, stage_table: str) -> tuple[tuple[str, str], ...]:
    resources = tuple(
        resource for resource in state.owned_stage_resources if resource[1] == stage_table
    )
    if not resources:
        message = "A native assignment checkpoint has no acknowledged owned resource."
        raise ValueError(message)
    return resources


def _scoped_stage_resources(
    state: TransferStageState, stage_table: str
) -> tuple[tuple[CreatedTableResource, str], ...]:
    return tuple(item for item in state.owned_stage_scoped_resources if item[1] == stage_table)


def retained_assignment_resources(options: TransferOptions, *, failed: bool) -> frozenset[str]:
    cached = cast(
        "_RetainedAssignment | None", options.input_resolution.get("native_assignment_checkpoint")
    )
    if not failed or cached is None:
        return frozenset()
    return frozenset(resource for resource, _stage in cached.owned_resources)


def native_cleanup_arguments(options: TransferOptions, *, failed: bool) -> dict[str, Any]:
    resources = retained_assignment_resources(options, failed=failed)
    return {"retained_resources": resources} if resources else {}


def complete_assignment_publication(options: TransferOptions) -> None:
    options.input_resolution.pop("native_assignment_checkpoint", None)


def assignment_replacement_schema(
    options: TransferOptions, current: dict[str, str] | None
) -> dict[str, str] | None:
    certificate = cast(
        "NativeAssignmentCertificate | None",
        options.input_resolution.get("native_assignment_certificate"),
    )
    return (
        dict(certificate.column_types)
        if certificate is not None and options.replace_target_table
        else current
    )


def close_assignment_checkpoint(options: TransferOptions) -> None:
    """Release terminal owned checkpoints without hiding an active primary error."""
    cached = cast(
        "_RetainedAssignment | None",
        options.input_resolution.pop("native_assignment_checkpoint", None),
    )
    if cached is None:
        return
    if preserve_recovery_artifacts():
        time_print(
            "Retaining native assignment checkpoint "
            f"{cached.certificate.stage_table} for structural recovery."
        )
        return
    from . import adaptive_input as adaptive  # noqa: PLC0415 -- frozen orchestration import cycle.

    primary = sys.exc_info()[1]
    if not cached.scoped_resources:
        time_print(
            f"Retaining native assignment checkpoint {cached.certificate.stage_table}: "
            "no acknowledged creation identity establishes ownership."
        )
        return
    for resource, stage in reversed(cached.scoped_resources):

        def drop(
            ref: dict[str, Any],
            *,
            resource: CreatedTableResource = resource,
            stage: str = stage,
        ) -> None:
            drop_owned_resource(
                get_backend_adapter("gp"),
                ref["connection"],
                resource,
                logical_stage=stage,
                creation_policy=None,
            )

        try:
            adaptive.require_frozen_configuration(options)
            run_with_fresh_connection(
                options.to_db_key,
                "cleanup_native_assignment_checkpoint",
                drop,
            )
        except Exception as error:
            if primary is None:
                raise
            time_print(
                f"Retained native assignment checkpoint {stage}: {type(error).__name__}.",
                level="warning",
            )


def annotate_assignment_metadata(options: TransferOptions, metadata: Any) -> None:
    certificate = cast(
        "NativeAssignmentCertificate | None",
        options.input_resolution.get("native_assignment_certificate"),
    )
    if certificate is None:
        return
    destination_types = dict(certificate.column_types)
    for record in metadata.type_resolutions:
        target_type = destination_types[record["column"]]
        record.update(
            native_assignment_validation="certified",
            native_assignment_destination_type=target_type,
        )
        if record.get("native_assignment_required") or requires_native_assignment(
            target_type, "gp"
        ):
            record.update(
                native_assignment_required=False,
                reason="native_assignment_checkpoint",
                certified_input_count=certificate.row_count,
            )
