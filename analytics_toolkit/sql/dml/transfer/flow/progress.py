from __future__ import annotations

from typing import Any

from ..runtime.models import TransferOptions


_TRANSFER_PROGRESS_UNKNOWN_TOTAL_FORMAT = (
    "{desc}: {n_pretty}{unit} [{elapsed}, {rate_fmt}{postfix}]"
)
_TRANSFER_PROGRESS_TOTAL_FORMAT = (
    "{l_bar}{bar}| {n_pretty}/{total_pretty} "
    "[{elapsed}<{remaining}, {rate_fmt}{postfix}]"
)


def make_transfer_progress_bar(
    options: TransferOptions,
    *,
    total: int | None,
    base_tqdm: Any,
) -> Any:
    tqdm_class = make_transfer_progress_bar_class(base_tqdm)
    bar_format = (
        _TRANSFER_PROGRESS_TOTAL_FORMAT
        if total is not None
        else _TRANSFER_PROGRESS_UNKNOWN_TOTAL_FORMAT
    )
    return tqdm_class(
        total=total,
        desc=f"transfer_table {options.to_db_key}.{options.target_table}",
        unit="row",
        disable=not options.progress,
        bar_format=bar_format,
    )


def make_transfer_progress_bar_class(base_tqdm: Any) -> Any:
    class TransferProgressTqdm(base_tqdm):
        @property
        def format_dict(self) -> dict[str, Any]:
            format_dict = super().format_dict
            total = format_dict.get("total")
            format_dict["n_pretty"] = format_transfer_progress_count(
                format_dict.get("n", self.n)
            )
            format_dict["total_pretty"] = (
                "?" if total is None else format_transfer_progress_count(total)
            )
            return format_dict

    return TransferProgressTqdm


def format_transfer_progress_count(value: Any) -> str:
    return f"{value:_}"
