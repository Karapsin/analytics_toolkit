from __future__ import annotations

# ruff: noqa: EM102, TID252, TRY003
from typing import Any

from ..runtime.models import TransferConcurrency

CONCURRENCY_CONFLICT_ERROR = (
    "concurrency cannot be combined with read_concurrency or write_concurrency; "
    "use either the legacy combined setting or the split settings."
)
SPLIT_CONCURRENCY_SCOPE_ERROR = (
    "read_concurrency and write_concurrency are supported only for keyed transfers "
    "using transfer_keys and transfer_key_values."
)


def validate_concurrency_value(value: Any, name: str) -> None:
    if value is None:
        return
    if isinstance(value, bool) or type(value) is not int or value < 1:
        raise ValueError(f"{name} must be a positive integer.")


def resolve_transfer_concurrency(
    *,
    concurrency: Any,
    read_concurrency: Any,
    write_concurrency: Any,
    slice_count: int | None,
    direct_keyed: bool,
) -> TransferConcurrency:
    for name, value in (
        ("concurrency", concurrency),
        ("read_concurrency", read_concurrency),
        ("write_concurrency", write_concurrency),
    ):
        validate_concurrency_value(value, name)
    split_requested = read_concurrency is not None or write_concurrency is not None
    if concurrency is not None and split_requested:
        raise ValueError(CONCURRENCY_CONFLICT_ERROR)
    if split_requested and not direct_keyed:
        raise ValueError(SPLIT_CONCURRENCY_SCOPE_ERROR)

    requested_read = concurrency if concurrency is not None else read_concurrency or 1
    requested_write = concurrency if concurrency is not None else write_concurrency or 1
    effective_read = requested_read
    effective_write = requested_write
    if direct_keyed and slice_count is not None:
        effective_read = min(effective_read, slice_count)
        effective_write = min(effective_write, slice_count)
    return TransferConcurrency(
        legacy_value=concurrency,
        requested_read=requested_read,
        requested_write=requested_write,
        effective_read=effective_read,
        effective_write=effective_write,
        split_requested=split_requested,
    )
