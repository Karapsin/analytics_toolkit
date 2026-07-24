from __future__ import annotations

# ruff: noqa: EM102, I001, TID252, TRY003

import hashlib
from dataclasses import dataclass
from typing import Iterable

from ....core.identifiers import TableIdentifier, quote_identifier_part
from ....backends.transfer_stage import normalize_unquoted_identifier


TRANSFER_ID_COLUMN = "__analytics_toolkit_transfer_id"
DESTINATION_COLUMN = "__analytics_toolkit_destination_table"
SLICE_ID_COLUMN = "__analytics_toolkit_slice_id"
ROW_ORDINAL_COLUMN = "__analytics_toolkit_row_ordinal"


@dataclass(frozen=True)
class DestinationIdentity:
    canonical: str
    fingerprint: str
    hash_prefix: str


@dataclass(frozen=True)
class TransferInternalColumns:
    transfer_id: str
    destination_table: str
    slice_id: str
    row_ordinal: str

    def names(self) -> tuple[str, str, str, str]:
        return (
            self.transfer_id,
            self.destination_table,
            self.slice_id,
            self.row_ordinal,
        )

    def quoted(self, backend: str) -> tuple[str, str, str, str]:
        return tuple(quote_identifier_part(name, backend) for name in self.names())  # type: ignore[return-value]


def resolve_destination_identity(table_name: str, backend: str) -> DestinationIdentity:
    identifier = TableIdentifier.parse(table_name, backend)
    parts = [
        quote_identifier_part(
            part if quoted else normalize_unquoted_identifier(part, backend),
            backend,
            quoted=quoted,
        )
        for part, quoted in zip(identifier.parts, identifier.quoted)
    ]
    canonical = ".".join(parts)
    fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return DestinationIdentity(
        canonical=canonical,
        fingerprint=fingerprint,
        hash_prefix=fingerprint[:16],
    )


def resolve_internal_columns(
    source_names: Iterable[str],
    backend: str,
    *,
    table_schema_names: Iterable[str] = (),
) -> TransferInternalColumns:
    occupied = {
        normalize_unquoted_identifier(name, backend)
        for name in (*tuple(source_names), *tuple(table_schema_names))
    }

    def allocate(base: str) -> str:
        candidate = base
        suffix = 0
        while normalize_unquoted_identifier(candidate, backend) in occupied:
            suffix += 1
            candidate = f"{base}_{suffix}"
        occupied.add(normalize_unquoted_identifier(candidate, backend))
        return candidate

    return TransferInternalColumns(
        transfer_id=allocate(TRANSFER_ID_COLUMN),
        destination_table=allocate(DESTINATION_COLUMN),
        slice_id=allocate(SLICE_ID_COLUMN),
        row_ordinal=allocate(ROW_ORDINAL_COLUMN),
    )


def assert_transfer_identity(
    *,
    expected_transfer_id: str,
    actual_transfer_id: str,
    expected_destination: str,
    actual_destination: str,
    resource: str,
) -> None:
    if actual_transfer_id != expected_transfer_id:
        raise RuntimeError(
            f"Transfer stage integrity failure for {resource}: transfer ID "
            f"{actual_transfer_id!r} does not match {expected_transfer_id!r}."
        )
    if actual_destination != expected_destination:
        raise RuntimeError(
            f"Transfer stage integrity failure for {resource}: destination "
            f"{actual_destination!r} does not match {expected_destination!r}."
        )
