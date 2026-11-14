"""Certify the client writer before publishing one selected transfer payload."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from analytics_toolkit.general import time_print
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.client_validation import (
    requires_client_validation,
    supports_client_validation,
    validate_client_write_capabilities,
)
from analytics_toolkit.sql.core.type_resolution import declared_domain_fits

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence

    from analytics_toolkit.sql.backends.ch.client_validation import ClientWriteValidation
    from analytics_toolkit.sql.backends.models import SourceColumn
    from analytics_toolkit.sql.dml.transfer.runtime.models import (
        TransferOptions,
        TransferStageState,
    )

    from .adaptive_input import AdaptiveTransferContext

_PLAN = "client_write_plan"
_CERTIFICATE = "client_write_certificate"
_CERTIFICATES = "client_write_certificates"


class ClientStageValidationError(ValueError):
    """A created stage cannot safely receive the already certified payload."""

    analytics_toolkit_sql_retry_safe = False


def client_validation_plan_options(options: TransferOptions) -> dict[str, Any]:
    """Describe unresolved encoder work without importing or constructing an SDK."""
    if not supports_client_validation(options.to_db_backend):
        return {}
    return {
        "client_write_validation_pending": True,
        "client_write_value_validation_pending": requires_client_validation(
            options.to_db_backend, options.table_schema or {}
        )
        or options.table_schema is None,
        "client_write_validation_basis": "declared_source_domain_or_complete_frozen_input",
    }


def prepare_client_write_validation(
    options: TransferOptions,
    connection: Any,
    source_schema: Sequence[SourceColumn],
    incoming_schema: Mapping[str, str],
    *,
    authoritative: bool,
) -> bool:
    """Check factories first and request a full checkpoint only when needed."""
    if not supports_client_validation(options.to_db_backend):
        return False
    source_types = {column.name: column.native_type or "" for column in source_schema}
    plan: dict[str, Any] = {
        "incoming_schema": dict(incoming_schema),
        "source_schema": source_types,
        "capability": None,
    }
    spool = options.input_resolution.get("spool")
    if spool is not None and spool.certified and spool.row_count == 0:
        options.input_resolution[_PLAN] = plan
        return False
    if not authoritative and not all(
        declared_domain_fits(
            source_types[name],
            native,
            options.to_db_backend,
            source_backend=options.from_db_backend,
        )
        for name, native in incoming_schema.items()
    ):
        # Inferred candidates are unresolved until the complete profile chooses
        # their final representation. An unsupported candidate is not final DDL.
        options.input_resolution[_PLAN] = plan
        return False
    try:
        capability = cast(
            "ClientWriteValidation",
            validate_client_write_capabilities(
                options.to_db_backend,
                connection,
                incoming_schema,
                writer_kind="rows",
                prospective_schema=True,
            ),
        )
    except ValueError as exc:
        # No client codec is needed for a genuinely empty SQL-only creation.
        # Keep the sanitized failure until the first selected nonempty batch.
        plan["factory_error"] = str(exc)
        options.input_resolution[_PLAN] = plan
        return True
    if capability.encoded_columns and not capability.opaque_columns:
        # A failed domain proof requests profiling. A factory failure is kept
        # separately and rejected at the first nonempty input, before target DDL.
        try:
            proven = validate_client_write_capabilities(
                options.to_db_backend,
                connection,
                incoming_schema,
                source_backend=options.from_db_backend,
                source_schema=source_types,
                writer_kind="rows",
                prospective_schema=True,
            )
        except ValueError:
            proven = None
        if proven is not None:
            capability = proven
    options.input_resolution[_PLAN] = {
        "incoming_schema": dict(incoming_schema),
        "source_schema": source_types,
        "capability": capability,
    }
    return bool(
        capability.encoded_columns and capability.validation_basis != "declared-source-domain"
    )


def raise_pending_client_write_error(options: TransferOptions, rows: Sequence[Any]) -> None:
    """Reject an unsupported writer only after a client write is actually needed."""
    pending = options.input_resolution.get(_PLAN, {}).get("factory_error")
    if rows and pending is not None:
        raise ClientStageValidationError(pending)


def _converted_batches(options: TransferOptions) -> Iterator[list[tuple[Any, ...]]]:
    from . import adaptive_input as adaptive  # noqa: PLC0415 -- shared publication geometry.

    for batch in adaptive.resolved_source_batches(options, None):
        yield batch.rows


def _publication_geometry(options: TransferOptions) -> tuple[Any, ...]:
    # The frozen iterator samples the initial batch size once and publishes one
    # merged selected spool, exactly as run_resolved_checkpoint_transfer does.
    return (
        "resolved_source_batches",
        options.batch_size,
        options.min_batch_size,
        options.max_batch_size,
        options.adaptive_batch_size,
        "merged_frozen_spool",
    )


def _certify(
    options: TransferOptions,
    connection: Any,
    incoming_schema: Mapping[str, str],
    *,
    physical_schema: Mapping[str, str] | None = None,
    prospective_schema: bool = True,
) -> None:
    cached = options.input_resolution
    plan = cached[_PLAN]
    spool = cached.get("spool")
    if spool is not None and spool.certified and spool.row_count == 0:
        cached.pop(_CERTIFICATE, None)
        plan["empty_input"] = True
        return
    capability = cast(
        "ClientWriteValidation",
        validate_client_write_capabilities(
            options.to_db_backend,
            connection,
            incoming_schema,
            physical_schema=physical_schema,
            writer_kind="rows",
            prospective_schema=prospective_schema,
        ),
    )
    plan["capability"] = capability
    identity = (
        capability.driver,
        capability.column_types,
        tuple(incoming_schema.items()),
        None if spool is None else spool.digest,
        _publication_geometry(options),
    )
    certificates = cached.setdefault(_CERTIFICATES, {})
    previous = certificates.get(identity)
    if previous is not None:
        previous[1].require_complete(0 if spool is None else spool.row_count)
        cached[_CERTIFICATE] = previous
        return
    if spool is not None and capability.encoded_columns:
        capability = cast(
            "ClientWriteValidation",
            validate_client_write_capabilities(
                options.to_db_backend,
                connection,
                incoming_schema,
                physical_schema=physical_schema,
                batches=_converted_batches(options),
                writer_kind="rows",
                prospective_schema=prospective_schema,
            ),
        )
        capability.require_complete(spool.row_count)
    elif capability.encoded_columns:
        capability = cast(
            "ClientWriteValidation",
            validate_client_write_capabilities(
                options.to_db_backend,
                connection,
                incoming_schema,
                physical_schema=physical_schema,
                source_backend=options.from_db_backend,
                source_schema=plan["source_schema"],
                writer_kind="rows",
                prospective_schema=prospective_schema,
            ),
        )
        capability.require_complete(0)
    certificate = identity, capability
    certificates[identity] = certificate
    cached[_CERTIFICATE] = certificate


def certify_frozen_client_input(
    options: TransferOptions,
    context: AdaptiveTransferContext,
) -> None:
    """Validate these replay rows before releasing their native source snapshot."""
    plan = options.input_resolution.get(_PLAN)
    if plan is None:
        return
    cached = options.input_resolution
    if cached["spool"].certified and cached["spool"].row_count == 0:
        cached.pop(_CERTIFICATE, None)
        plan["empty_input"] = True
        return
    plan["incoming_schema"] = dict(
        zip(cached["spool"].columns, (item.target_type for item in cached["resolutions"]))
    )
    from . import adaptive_input as adaptive  # noqa: PLC0415 -- orchestration cycle.

    try:
        adaptive.run_target_operation(
            options,
            "certify_client_input",
            lambda ref: _certify(options, ref["connection"], plan["incoming_schema"]),
            open_connection=context.open_connection or adaptive.get_sql_connection,
        )
    except BaseException:
        native = cached.get("native_snapshot")
        if native is not None:
            time_print(
                f"Retained source checkpoint {native[0]}: "
                "selected client writer was not certified.",
                level="warning",
            )
        raise


def require_client_write_certificate(
    options: TransferOptions, connection: Any, state: TransferStageState
) -> None:
    """A fresh destination must honor the certified writer and input identity."""
    if _PLAN not in options.input_resolution:
        return
    _certify(options, connection, state.stage_column_types or {})


def validate_created_client_stage(
    options: TransferOptions, connection: Any, state: TransferStageState
) -> None:
    """Check acknowledged stage metadata before its first Python client batch."""
    plan = options.input_resolution.get(_PLAN)
    if plan is None:
        return
    capability = plan["capability"]
    if capability is not None and not capability.encoded_columns:
        return
    incoming = state.stage_column_types or {}
    actual = get_backend_adapter(options.to_db_backend).get_table_column_types(
        connection,
        state.stage_table or "",
        connection_key=options.to_db_key,
    )
    expected = plan["incoming_schema"]
    equivalent = list(expected) == list(actual) and all(
        declared_domain_fits(
            native, actual[name], options.to_db_backend, source_backend=options.to_db_backend
        )
        and declared_domain_fits(
            actual[name], native, options.to_db_backend, source_backend=options.to_db_backend
        )
        for name, native in expected.items()
    )
    if not equivalent:
        message = "Created transfer stage differs from its certified client writer schema."
        raise ClientStageValidationError(message)
    _certify(options, connection, incoming, physical_schema=actual, prospective_schema=False)
