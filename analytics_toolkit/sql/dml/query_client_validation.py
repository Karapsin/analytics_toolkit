"""Certify the selected writer only when query creation publishes Python values."""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, cast

from analytics_toolkit.sql.backends.client_validation import (
    supports_client_validation,
    validate_client_write_capabilities,
)
from analytics_toolkit.sql.dml.transfer.flow.frozen_retry import frozen_input_retry

guarded_query_retry = frozen_input_retry

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from analytics_toolkit.sql.backends.ch.client_validation import ClientWriteValidation
    from analytics_toolkit.sql.core.replay_spool import ReplaySpool
    from analytics_toolkit.sql.dml.query_input import PreparedQueryInput
    from analytics_toolkit.sql.dml.transfer.runtime.models import TransferOptions
    from analytics_toolkit.sql.execution.plans import SqlOperationMetadata


@contextmanager
def query_validation_connection(
    options: TransferOptions, connection_ref: dict[str, Any]
) -> Iterator[None]:
    """Use the query's selected target session for its internal validation."""
    from .transfer.flow.adaptive_input import (  # noqa: PLC0415 -- shared orchestration cycle.
        require_frozen_configuration,
    )

    cached = options.input_resolution
    missing = object()
    previous = cached.get("target_connection_runner", missing)

    def runner(_role: str, operation: Any) -> Any:
        require_frozen_configuration(options)
        return operation(connection_ref)

    cached["target_connection_runner"] = runner
    try:
        yield
    finally:
        if previous is missing:
            cached.pop("target_connection_runner", None)
        else:
            cached["target_connection_runner"] = previous


def _converted_batches(prepared: PreparedQueryInput) -> Iterator[Sequence[Sequence[Any]]]:
    from .transfer.flow.adaptive_input import (  # noqa: PLC0415 -- query/transfer orchestration cycle.
        resolved_source_batches,
    )

    for batch in resolved_source_batches(prepared.options, iter):
        yield batch.rows


def validate_query_client_input(
    prepared: PreparedQueryInput,
    connection_ref: dict[str, Any],
    *,
    python_consumer: bool,
) -> None:
    """Validate the actual SDK factory and the same frozen rows used by replay.

    Ordinary native INSERT SELECT routes never touch a client encoder. A lazy
    client route can retain its metadata proof, with an unknown selected count.
    """
    if not python_consumer:
        return
    options = prepared.options
    if not supports_client_validation(options.to_db_backend):
        return
    cached = options.input_resolution
    spool = cast("ReplaySpool | None", cached.get("spool"))
    complete = spool is not None and spool.certified
    if prepared.frozen and spool is not None and spool.certified and spool.row_count == 0:
        cached.pop("query_client_write_validation", None)
        cached["query_empty_client_input"] = True
        return
    cached.pop("query_empty_client_input", None)
    batches = _converted_batches(prepared) if prepared.frozen and complete else None
    source_types = {
        name: native_type
        for name, native_type in (prepared.state.source_column_types or {}).items()
        if native_type is not None
    }
    certificate = cast(
        "ClientWriteValidation",
        validate_client_write_capabilities(
            options.to_db_backend,
            connection_ref["connection"],
            prepared.schema,
            batches=batches,
            source_backend=None if prepared.frozen else options.from_db_backend,
            source_schema=None if prepared.frozen else source_types,
            writer_kind="rows",
            prospective_schema=True,
        ),
    )
    if prepared.frozen:
        if spool is None or not spool.certified:
            message = "Client query replay requires a complete certified input checkpoint."
            raise ValueError(message)
        certificate.require_complete(spool.row_count)
    elif certificate.encoded_columns and certificate.validation_basis != "declared-source-domain":
        message = "Client query aliases require a complete frozen input or a declared-domain proof."
        raise ValueError(message)
    cached["query_client_write_validation"] = certificate


def describe_query_client_validation(
    prepared: PreparedQueryInput, metadata: SqlOperationMetadata
) -> None:
    certificate = cast(
        "ClientWriteValidation | None",
        prepared.options.input_resolution.get("query_client_write_validation"),
    )
    driver: str | None
    count: int | None
    if certificate is None:
        if not prepared.options.input_resolution.get("query_empty_client_input"):
            return
        column_types = tuple(prepared.schema.items())
        basis, driver, count = "certified-empty-input", None, 0
    else:
        column_types = certificate.column_types
        basis = certificate.validation_basis
        driver = certificate.driver
        count = certificate.certified_input_count
    if metadata.type_resolutions is None:
        metadata.type_resolutions = [
            {
                "column": name,
                "source_type": (prepared.state.source_column_types or {}).get(name),
                "target_type": native_type,
            }
            for name, native_type in column_types
        ]
    for record in metadata.type_resolutions:
        record.update(
            client_validation_basis=basis,
            client_validation_driver=driver,
            client_validation_certified_count=count,
        )
