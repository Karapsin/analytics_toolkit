"""Backend-dispatched client writer preflight, independent of SQL orchestration."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from .ch.client_validation import ClientWriteValidation


def supports_client_validation(backend: str) -> bool:
    """Identify the backend contract without importing an optional client SDK."""
    return backend == "ch"


def requires_client_validation(backend: str, schema: Mapping[str, str]) -> bool:
    """Report pending client validation without importing optional SDK packages."""
    if not supports_client_validation(backend):
        return False
    # Resolve the selected backend policy lazily.
    from .ch.client_validation import (  # noqa: PLC0415
        requires_client_write_validation,
    )

    return requires_client_write_validation(schema)


def validate_client_write_capabilities(  # noqa: PLR0913 -- internal backend selection and frozen-input contract.
    backend: str,
    connection: Any,
    incoming_schema: Mapping[str, str],
    *,
    physical_schema: Mapping[str, str] | None = None,
    rows: Iterable[Sequence[Any]] | None = None,
    batches: Iterable[Any] | None = None,
    source_backend: str | None = None,
    source_schema: Mapping[str, str] | None = None,
    writer_kind: str = "dataframe",
    prospective_schema: bool = False,
) -> ClientWriteValidation | None:
    if not supports_client_validation(backend):
        return None
    # Keep selected-backend implementation imports out of offline planning.
    from .ch import client_validation as ch_client  # noqa: PLC0415

    schema = ch_client.effective_client_write_schema(
        connection, incoming_schema, physical_schema, writer_kind=writer_kind
    )
    certificate = ch_client.validate_client_write_capabilities(
        connection,
        schema,
        rows=rows,
        batches=batches,
        normalization_schema=incoming_schema,
        writer_kind=writer_kind,
        prospective_schema=prospective_schema,
    )
    if rows is None and batches is None and source_backend == "ch" and source_schema is not None:
        certificate = certificate.prove_declared_domain(
            source_schema,
            intermediate_schema=incoming_schema,
        )
    return certificate
