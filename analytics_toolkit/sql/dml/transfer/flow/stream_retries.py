from __future__ import annotations

from dataclasses import replace

from analytics_toolkit.general import time_print

from ..io.source import TransferSourceStreamReadError
from ..runtime.models import TransferOptions


class TransferStreamRetryState:
    def __init__(self, options: TransferOptions) -> None:
        self.options = options
        self.failure_count = 0

    def options_for_attempt(self) -> TransferOptions:
        return options_for_stream_read_retry(self.options, self.failure_count)

    def handle_failure(
        self,
        exc: TransferSourceStreamReadError,
        *,
        attempt_options: TransferOptions,
        attempt: int,
    ) -> None:
        exc.with_retry_context(
            target_table=attempt_options.target_table,
            retry_batch_size=attempt_options.batch_size,
            full_retry_attempt=attempt,
        )
        self.failure_count += 1
        if attempt < self.options.full_retry_cnt:
            next_options = options_for_stream_read_retry(
                self.options,
                self.failure_count,
            )
            time_print(
                "Retrying transfer from scratch after ClickHouse source "
                "stream read failure with "
                f"batch_size={next_options.batch_size}",
                level="warning",
            )


def options_for_stream_read_retry(
    options: TransferOptions,
    stream_read_failure_count: int,
) -> TransferOptions:
    if stream_read_failure_count <= 0:
        return options
    batch_size = max(
        options.min_batch_size,
        options.batch_size // (2 ** stream_read_failure_count),
    )
    max_batch_size = (
        batch_size
        if options.max_batch_size is None
        else min(options.max_batch_size, batch_size)
    )
    return replace(options, batch_size=batch_size, max_batch_size=max_batch_size)
