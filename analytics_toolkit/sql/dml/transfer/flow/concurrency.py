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
_DEFAULT_HARD_CONCURRENCY_CAP = 5


def validate_concurrency_value(value: Any, name: str) -> None:
    if value is None:
        return
    if isinstance(value, bool) or type(value) is not int or value < 1:
        raise ValueError(f"{name} must be a positive integer.")


def validate_transfer_concurrency_ceilings(
    *,
    concurrency: Any,
    read_concurrency: Any,
    write_concurrency: Any,
    soft_concurrency_cap: Any,
    hard_concurrency_cap: Any,
) -> tuple[int, int, int, int]:
    for name, value in (
        ("concurrency", concurrency),
        ("read_concurrency", read_concurrency),
        ("write_concurrency", write_concurrency),
        ("soft_concurrency_cap", soft_concurrency_cap),
    ):
        validate_concurrency_value(value, name)
    if type(hard_concurrency_cap) is not int or hard_concurrency_cap < 1:
        message = "hard_concurrency_cap must be a positive integer."
        raise ValueError(message)

    split_requested = read_concurrency is not None or write_concurrency is not None
    if concurrency is not None and split_requested:
        raise ValueError(CONCURRENCY_CONFLICT_ERROR)

    requested_read = concurrency if concurrency is not None else read_concurrency or 1
    requested_write = concurrency if concurrency is not None else write_concurrency or 1
    soft_limited_read = (
        requested_read
        if soft_concurrency_cap is None
        else min(requested_read, soft_concurrency_cap)
    )
    soft_limited_write = (
        requested_write
        if soft_concurrency_cap is None
        else min(requested_write, soft_concurrency_cap)
    )
    if soft_limited_read > hard_concurrency_cap or soft_limited_write > hard_concurrency_cap:
        raise ValueError(
            "effective transfer concurrency exceeds hard_concurrency_cap "
            f"(read/write {soft_limited_read}/{soft_limited_write}; "
            f"hard cap {hard_concurrency_cap}). Reduce concurrency, set "
            "soft_concurrency_cap at or below hard_concurrency_cap, or increase "
            "hard_concurrency_cap."
        )
    return requested_read, requested_write, soft_limited_read, soft_limited_write


def resolve_transfer_concurrency(  # noqa: PLR0913 -- preserve the internal compatibility API
    *,
    concurrency: Any,
    read_concurrency: Any,
    write_concurrency: Any,
    soft_concurrency_cap: Any = None,
    hard_concurrency_cap: Any = _DEFAULT_HARD_CONCURRENCY_CAP,
    slice_count: int | None,
    direct_keyed: bool,
) -> TransferConcurrency:
    (
        requested_read,
        requested_write,
        soft_limited_read,
        soft_limited_write,
    ) = validate_transfer_concurrency_ceilings(
        concurrency=concurrency,
        read_concurrency=read_concurrency,
        write_concurrency=write_concurrency,
        soft_concurrency_cap=soft_concurrency_cap,
        hard_concurrency_cap=hard_concurrency_cap,
    )
    split_requested = read_concurrency is not None or write_concurrency is not None
    if split_requested and not direct_keyed:
        raise ValueError(SPLIT_CONCURRENCY_SCOPE_ERROR)

    effective_read = soft_limited_read
    effective_write = soft_limited_write
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
        soft_concurrency_cap=soft_concurrency_cap,
        hard_concurrency_cap=hard_concurrency_cap,
        soft_limited_read=soft_limited_read,
        soft_limited_write=soft_limited_write,
    )
