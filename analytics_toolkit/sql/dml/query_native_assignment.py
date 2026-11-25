"""Own complete native query assignments before creation touches a live target."""

from __future__ import annotations

import hashlib
import sys
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

import pandas as pd

from analytics_toolkit.general import time_print
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.creation_resources import (
    require_created_resource,
    supports_created_resource_identity,
)
from analytics_toolkit.sql.backends.gp.native_assignment import (
    NativeAssignmentCertificate,
    certify_assignment_checkpoint,
    native_assignment_errors,
)
from analytics_toolkit.sql.backends.native_values import requires_native_assignment
from analytics_toolkit.sql.backends.stage_ownership import drop_owned_resource
from analytics_toolkit.sql.connection.get_sql_connection import get_sql_connection
from analytics_toolkit.sql.core.replay_spool import FrozenInputIdentity
from analytics_toolkit.sql.ddl.creation_resources import (
    CreatedTableResource,
    configuration_digest,
    execute_scoped_create,
    scoped_creation_observer,
)
from analytics_toolkit.sql.dml.load.stage import (
    STAGE_TABLE_NAME_MAX_ATTEMPTS,
    build_stage_table_name,
    create_stage_table,
)
from analytics_toolkit.sql.dml.transfer.flow.assignment_context import (
    require_pending_assignment_context,
)
from analytics_toolkit.sql.dml.transfer.flow.stage_ownership import stage_creation_callbacks
from analytics_toolkit.sql.dml.transfer.runtime.models import (
    TransferConnectionRefs,
    TransferStageState,
)
from analytics_toolkit.sql.dml.transfer.runtime.retry import (
    is_non_retryable_sql_error,
    run_with_fresh_connection,
)
from analytics_toolkit.sql.dml.transfer.schema import inspect_source_query_schema
from analytics_toolkit.sql.execution.backend_options import preserve_recovery_artifacts

if TYPE_CHECKING:
    from analytics_toolkit.sql.backends.models import SourceColumn
    from analytics_toolkit.sql.dml.query_input import PreparedQueryInput
    from analytics_toolkit.sql.dml.transfer.runtime.models import TransferOptions
    from analytics_toolkit.sql.execution.plans import SqlOperationMetadata


@dataclass
class _OwnedQueryAssignment:
    identity: FrozenInputIdentity
    destination_types: tuple[tuple[str, str], ...]
    state: TransferStageState = field(
        default_factory=lambda: TransferStageState(target_exists=False)
    )
    source_stage: str | None = None
    source_certificate: NativeAssignmentCertificate | None = None
    certificate: NativeAssignmentCertificate | None = None


def requires_query_assignment(schema: dict[str, str], backend: str) -> bool:
    return any(requires_native_assignment(native, backend) for native in schema.values())


def query_assignment_recoverable(options: TransferOptions) -> bool:
    owned = cast(
        "_OwnedQueryAssignment | None", options.input_resolution.get("query_native_assignment")
    )
    spool = options.input_resolution.get("spool")
    return (owned is not None and owned.source_stage is not None) or (
        spool is not None and spool.certified
    )


def _require_configuration(options: TransferOptions) -> None:
    from .transfer.flow.adaptive_input import (  # noqa: PLC0415 -- shared orchestration cycle.
        require_frozen_configuration,
    )

    require_frozen_configuration(options)


def _identity(options: TransferOptions) -> FrozenInputIdentity:
    spool = options.input_resolution.get("spool")
    if spool is not None:
        return cast("FrozenInputIdentity", spool.identity)
    return FrozenInputIdentity(
        source_key=options.from_db_key,
        configuration_digest=options.input_resolution["configuration_digest"],
        namespace=(options.to_db_key, options.canonical_destination_identity or ""),
        query_digest=hashlib.sha256(options.source_sql.encode()).hexdigest(),
        destination_timezone=options.input_resolution.get("destination_timezone"),
        timezone_policy_version=options.input_resolution.get("timezone_policy_version"),
    )


def _owned_input(options: TransferOptions, schema: dict[str, str]) -> _OwnedQueryAssignment:
    _require_configuration(options)
    identity = _identity(options)
    cached = cast(
        "_OwnedQueryAssignment | None", options.input_resolution.get("query_native_assignment")
    )
    if cached is None:
        cached = _OwnedQueryAssignment(identity, tuple(schema.items()))
        options.input_resolution["query_native_assignment"] = cached
    if cached.identity != identity or cached.destination_types != tuple(schema.items()):
        message = "Query or destination schema differs from its native assignment checkpoint."
        raise ValueError(message)
    return cached


def _certify(  # noqa: PLR0913 -- bind one physical checkpoint to its full native schema.
    options: TransferOptions,
    connection: Any,
    stage: str,
    schema: dict[str, str],
    count: int,
    identity: FrozenInputIdentity,
) -> NativeAssignmentCertificate:
    _require_configuration(options)
    context = require_pending_assignment_context(options, connection)
    owned = cast("_OwnedQueryAssignment", options.input_resolution["query_native_assignment"])
    for resource, logical_stage in owned.state.owned_stage_scoped_resources:
        if logical_stage == stage:
            require_created_resource(
                get_backend_adapter(options.to_db_backend), connection, resource
            )
    return certify_assignment_checkpoint(
        get_backend_adapter(options.to_db_backend),
        connection,
        stage,
        schema,
        expected_rows=count,
        input_identity=identity,
        query_label=options.query_label,
        session_context=context,
    )


def _create_typed_stage(
    options: TransferOptions,
    owned: _OwnedQueryAssignment,
    connection: Any,
    schema: dict[str, str],
) -> str:
    _require_configuration(options)
    with native_assignment_errors("creation", non_retryable_predicate=is_non_retryable_sql_error):
        stage = create_stage_table(
            options.to_db_backend,
            connection,
            options.target_table,
            pd.DataFrame(columns=list(schema)),
            column_types=schema,
            connection_key=options.to_db_key,
            query_label=options.query_label,
            transfer_staging_schema=options.transfer_staging_schema,
            destination_hash=options.destination_hash,
            ddl_properties=options.staging_ddl_properties,
            **stage_creation_callbacks(owned.state, options.to_db_backend),
        )
    _require_owned(owned, stage)
    return stage


def _require_owned(owned: _OwnedQueryAssignment, stage: str) -> None:
    if (stage, stage) not in owned.state.owned_stage_resources:
        message = "Native query assignment checkpoint has no acknowledged owned resource."
        raise ValueError(message)
    if owned.state.scoped_stage_ownership_requested and not any(
        logical_stage == stage
        for _resource, logical_stage in owned.state.owned_stage_scoped_resources
    ):
        message = "Native query assignment checkpoint has no scoped ownership certificate."
        raise ValueError(message)


def _materialize_native_query(
    options: TransferOptions, owned: _OwnedQueryAssignment, connection: Any
) -> str:
    adapter = get_backend_adapter(options.to_db_backend)
    for _attempt in range(STAGE_TABLE_NAME_MAX_ATTEMPTS):
        stage = build_stage_table_name(
            options.to_db_backend,
            options.target_table,
            transfer_staging_schema=options.transfer_staging_schema,
            random_suffix=uuid.uuid4().hex[:8],
            destination_hash=options.destination_hash,
        )
        if adapter.table_exists(connection, stage, connection_key=options.to_db_key):
            continue
        owned.state.stage_table_candidates.append(stage)
        _require_configuration(options)
        require_pending_assignment_context(options, connection)
        try:
            sql = adapter.build_materialize_transfer_source_sql(
                stage, options.source_sql, query_label=options.query_label
            )
            _execute_owned_materialization(options, owned, connection, stage, sql)
        except Exception as error:
            sqlstate = getattr(error, "pgcode", None) or getattr(error, "sqlstate", None)
            if sqlstate == "42P07":
                owned.state.stage_table_candidates.remove(stage)
                continue
            error.__dict__["analytics_toolkit_sql_stage_candidate"] = stage
            error.__dict__["analytics_toolkit_sql_acknowledged_resources"] = ()
            raise
        owned.source_stage = stage
        return stage
    message = "Could not allocate a collision-free native query checkpoint."
    raise ValueError(message)


def _execute_owned_materialization(
    options: TransferOptions, owned: _OwnedQueryAssignment, connection: Any, stage: str, sql: str
) -> None:
    adapter = get_backend_adapter(options.to_db_backend)
    if not supports_created_resource_identity(adapter):
        adapter.execute_command(connection, sql)
        owned.state.owned_stage_resources.append((stage, stage))
        return
    owned.state.scoped_stage_ownership_requested = True

    def acknowledged(resource: CreatedTableResource) -> None:
        owned.state.owned_stage_scoped_resources.append((resource, stage))
        owned.state.owned_stage_resources.append((resource.name, stage))

    resource = CreatedTableResource(
        stage,
        options.to_db_backend,
        connection_key=options.to_db_key,
        configuration_digest=configuration_digest(options.to_db_key),
    )
    with scoped_creation_observer(acknowledged):
        execute_scoped_create(adapter, connection, sql, resource)


def prepare_native_sql_assignment(
    options: TransferOptions,
    schema: dict[str, str],
    columns: list[str],
    connection_ref: dict[str, Any],
) -> None:
    """Preserve target-session SQL projection semantics without a Python round trip."""
    owned = _owned_input(options, schema)
    if owned.certificate is not None:
        require_query_assignment_certificate(options, schema, connection_ref)
        return
    connection = connection_ref["connection"]
    adapter = get_backend_adapter(options.to_db_backend)
    with native_assignment_errors("population", non_retryable_predicate=is_non_retryable_sql_error):
        stage = owned.source_stage or _materialize_native_query(options, owned, connection)
        projection = adapter.column_list_sql(columns)
        source_query = f"SELECT {projection} FROM {stage}"  # noqa: S608 -- owned quoted identifier.
        if owned.source_certificate is None:
            actual_schema = inspect_source_query_schema(
                options.to_db_backend, connection, source_query
            )
            if [column.name for column in actual_schema] != columns:
                message = "Native query checkpoint columns differ from the selected input."
                raise ValueError(message)
            actual_types: dict[str, str] = {}
            for column in actual_schema:
                native_type = column.native_type
                if native_type is None:
                    message = "Native query checkpoint requires complete native type metadata."
                    raise ValueError(message)
                actual_types[column.name] = native_type
            count = adapter.count_table_rows(connection, stage, query_label=options.query_label)
            owned.source_certificate = _certify(
                options,
                connection,
                stage,
                actual_types,
                count,
                owned.identity,
            )
        final_stage = _create_typed_stage(options, owned, connection, schema)
        # This is the same projection/CAST chain as the original native route.
        adapter.insert_from_query(
            connection, final_stage, source_query, schema, query_label=options.query_label
        )
        owned.certificate = _certify(
            options,
            connection,
            final_stage,
            schema,
            owned.source_certificate.row_count,
            owned.identity,
        )
    options.input_resolution["input_count"] = owned.certificate.row_count


def prepare_python_query_assignment(
    prepared: PreparedQueryInput,
    source_ref: dict[str, Any],
    target_ref: dict[str, Any],
) -> None:
    """Bind a complete frozen Python input through its real native writer once."""
    from .transfer.flow.attempt import load_stage_batches  # noqa: PLC0415 -- query/transfer cycle.

    options, schema = prepared.options, prepared.schema
    owned = _owned_input(options, schema)
    if owned.certificate is not None:
        require_query_assignment_certificate(options, schema, target_ref)
        return
    spool = options.input_resolution.get("spool")
    if spool is None or not spool.certified:
        message = "Native query assignment requires a complete certified input checkpoint."
        raise ValueError(message)
    owned.state.scoped_stage_ownership_requested = supports_created_resource_identity(
        get_backend_adapter(options.to_db_backend)
    )
    state = TransferStageState(
        target_exists=False,
        source_columns=list(prepared.columns),
        source_column_types=prepared.state.source_column_types,
        stage_column_types=schema,
        input_types_resolved=True,
        owned_stage_resources=owned.state.owned_stage_resources,
        owned_stage_scoped_resources=owned.state.owned_stage_scoped_resources,
        scoped_stage_ownership_requested=owned.state.scoped_stage_ownership_requested,
        stage_table_candidates=owned.state.stage_table_candidates,
    )

    def runner(_role: str, operation: Any) -> Any:
        _require_configuration(options)
        require_pending_assignment_context(options, target_ref["connection"])
        return operation(target_ref)

    options.input_resolution["target_connection_runner"] = runner
    try:
        with native_assignment_errors(
            "population", non_retryable_predicate=is_non_retryable_sql_error
        ):
            total = load_stage_batches(
                options,
                TransferConnectionRefs(source=source_ref, target=target_ref),
                state,
                options.retry_cnt,
                1,
            )
            stage = state.stage_table or _create_typed_stage(
                options, owned, target_ref["connection"], schema
            )
            if total != spool.row_count:
                message = "Native query assignment did not load the complete certified input."
                raise ValueError(message)
            _require_owned(owned, stage)
            owned.certificate = _certify(
                options, target_ref["connection"], stage, schema, total, owned.identity
            )
    finally:
        options.input_resolution.pop("target_connection_runner", None)


def require_query_assignment_certificate(
    options: TransferOptions, schema: dict[str, str], connection_ref: dict[str, Any]
) -> NativeAssignmentCertificate:
    cached = cast(
        "_OwnedQueryAssignment | None", options.input_resolution.get("query_native_assignment")
    )
    if cached is None or cached.certificate is None:
        message = "Native query assignment requires a certified owned checkpoint before replay."
        raise ValueError(message)
    owned = _owned_input(options, schema)
    certificate = cached.certificate
    context = require_pending_assignment_context(options, connection_ref["connection"])
    _require_owned(owned, certificate.stage_table)
    if (
        certificate.input_identity != owned.identity
        or certificate.column_types != owned.destination_types
        or certificate.session_context != context
    ):
        message = "Native query assignment certificate differs from the selected input or schema."
        raise ValueError(message)
    _certify(
        options,
        connection_ref["connection"],
        certificate.stage_table,
        dict(certificate.column_types),
        certificate.row_count,
        owned.identity,
    )
    return certificate


def replay_query_assignment(prepared: PreparedQueryInput, connection_ref: dict[str, Any]) -> int:
    options = prepared.options
    certificate = require_query_assignment_certificate(options, prepared.schema, connection_ref)
    adapter = get_backend_adapter(options.to_db_backend)
    adapter.execute_command(
        connection_ref["connection"],
        adapter.build_insert_from_stage_sql(
            options.target_table,
            certificate.stage_table,
            columns=prepared.columns,
            column_types=None,
            query_label=options.query_label,
        ),
    )
    return certificate.row_count


def describe_query_assignment(prepared: PreparedQueryInput, metadata: SqlOperationMetadata) -> bool:
    owned = cast(
        "_OwnedQueryAssignment | None",
        prepared.options.input_resolution.get("query_native_assignment"),
    )
    if owned is None or owned.certificate is None:
        return False
    count = owned.certificate.row_count
    metadata.input_count = metadata.source_rows = count
    metadata.type_resolutions = [
        {
            "column": column.name,
            "source_type": column.native_type,
            "target_type": prepared.schema[column.name],
            "reason": "native_assignment_checkpoint",
            "certified_input_count": count,
            "native_assignment_required": False,
            "native_assignment_validation": "certified",
            "native_assignment_destination_type": prepared.schema[column.name],
        }
        for column in cast("list[SourceColumn]", prepared.options.input_resolution["source_schema"])
    ]
    return True


def close_query_assignment(options: TransferOptions, *, retain_checkpoint: bool = False) -> None:
    owned = cast(
        "_OwnedQueryAssignment | None",
        options.input_resolution.get("query_native_assignment"),
    )
    if owned is None:
        return
    names = owned.state.owned_stage_resources
    unowned = set(owned.state.stage_table_candidates).difference(name for name, _ in names)
    if unowned:
        time_print(f"Retaining unacknowledged native query checkpoints: {sorted(unowned)}")
    if retain_checkpoint or preserve_recovery_artifacts():
        time_print(f"Retaining native query assignment checkpoints: {[name for name, _ in names]}")
        return
    options.input_resolution.pop("query_native_assignment", None)
    primary = sys.exc_info()[1]
    first_error: Exception | None = None
    adapter = get_backend_adapter(options.to_db_backend)
    identity_required = supports_created_resource_identity(adapter)
    resources: list[tuple[Any, str]] = list(
        owned.state.owned_stage_scoped_resources if identity_required else names
    )
    seen: set[Any] = set()
    for resource, stage in reversed(resources):
        key = (resource.name, resource.scope) if identity_required else resource
        if key in seen:
            continue
        seen.add(key)
        try:
            _require_configuration(options)

            def drop_checkpoint(
                ref: dict[str, Any], resource: Any = resource, stage: str = stage
            ) -> None:
                drop_owned_resource(
                    get_backend_adapter(options.to_db_backend),
                    ref["connection"],
                    resource,
                    logical_stage=stage,
                    creation_policy=None,
                )

            run_with_fresh_connection(
                options.to_db_key,
                "cleanup_query_assignment_checkpoint",
                drop_checkpoint,
                open_connection=get_sql_connection,
            )
        except Exception as error:  # noqa: BLE001 -- clean each acknowledged owned resource.
            first_error = first_error or error
            time_print(
                f"Retained native query assignment checkpoint {stage}: {type(error).__name__}.",
                level="warning",
            )
    if primary is None and first_error is not None:
        raise first_error
