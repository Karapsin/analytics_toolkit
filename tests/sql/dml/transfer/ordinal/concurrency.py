from __future__ import annotations

from tests.sql._support.transfer_ordinal import (
    Any,
    TransferConcurrency,
    TransferOptions,
    _staged_options,
    api,
)


def test_transfer_metadata_uses_range_reduced_unkeyed_concurrency(monkeypatch: Any) -> None:
    template = _staged_options(
        transfer_concurrency=TransferConcurrency(
            legacy_value=None,
            requested_read=8,
            requested_write=4,
            effective_read=2,
            effective_write=2,
            split_requested=True,
            soft_concurrency_cap=2,
            hard_concurrency_cap=5,
            soft_limited_read=2,
            soft_limited_write=2,
        )
    )
    monkeypatch.setattr(api, "build_transfer_options", lambda **_kwargs: template)

    def transfer_attempt(options: TransferOptions, **_kwargs: Any) -> int:
        concurrency = options.transfer_concurrency
        object.__setattr__(
            options,
            "transfer_concurrency",
            TransferConcurrency(
                legacy_value=concurrency.legacy_value,
                requested_read=concurrency.requested_read,
                requested_write=concurrency.requested_write,
                effective_read=1,
                effective_write=1,
                split_requested=concurrency.split_requested,
                soft_concurrency_cap=concurrency.soft_concurrency_cap,
                hard_concurrency_cap=concurrency.hard_concurrency_cap,
                soft_limited_read=concurrency.soft_limited_read,
                soft_limited_write=concurrency.soft_limited_write,
            ),
        )
        return 1

    monkeypatch.setattr(api, "run_transfer_attempt", transfer_attempt)
    monkeypatch.setattr(api, "best_effort_transfer_target_count", lambda *_args, **_kwargs: 1)

    result = api.transfer_table("source", "target", return_metadata=True)

    assert result.metadata.requested_read_concurrency == 8
    assert result.metadata.requested_write_concurrency == 4
    assert result.metadata.soft_limited_read_concurrency == 2
    assert result.metadata.soft_limited_write_concurrency == 2
    assert result.metadata.effective_read_concurrency == 1
    assert result.metadata.effective_write_concurrency == 1
