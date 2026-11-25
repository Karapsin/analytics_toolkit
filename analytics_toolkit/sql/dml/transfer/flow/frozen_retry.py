"""Keep each client batch retry inside the certified configuration scope."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from analytics_toolkit.sql.dml.transfer.runtime.models import TransferOptions


def frozen_input_retry(options: TransferOptions, retry: Callable[..., Any]) -> Callable[..., Any]:
    """Recheck frozen identity even when an inner retry retains its connection."""
    if getattr(options, "input_resolution", {}).get("configuration_digest") is None:
        return retry

    def guarded_retry(**kwargs: Any) -> Any:
        operation = kwargs["operation"]

        def checked_operation(attempt: int) -> Any:
            from . import adaptive_input as adaptive  # noqa: PLC0415 -- retry/orchestration cycle.

            adaptive.require_frozen_configuration(options)
            return operation(attempt)

        return retry(**{**kwargs, "operation": checked_operation})

    return guarded_retry
