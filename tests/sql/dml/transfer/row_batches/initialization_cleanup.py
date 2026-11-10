"""Source acquisition remains protected when target initialization fails."""

from __future__ import annotations

import pytest

from tests.sql._support.row_batches import (
    FakeTransferConnection,
    attempt_module,
    keys_module,
    make_progress_options,
)


@pytest.mark.parametrize("keyed", [False, True])
@pytest.mark.parametrize("failure_at", ["open", "state"])
@pytest.mark.parametrize("close_fails", [False, True])
def test_target_initialization_failure_closes_acquired_source_exactly_once(
    monkeypatch: pytest.MonkeyPatch, keyed: bool, failure_at: str, close_fails: bool
) -> None:
    source = FakeTransferConnection("source")
    target = FakeTransferConnection("target")
    failure = ConnectionError("target initialization failed")

    if close_fails:

        def fail_close() -> None:
            source.close_calls += 1
            message = "source close failed"
            raise OSError(message)

        monkeypatch.setattr(source, "close", fail_close)

    def open_connection(key: str) -> FakeTransferConnection:
        if key == "gp":
            return source
        if failure_at == "open":
            raise failure
        return target

    def create_state(*_args: object, **_kwargs: object) -> None:
        raise failure

    monkeypatch.setattr(attempt_module, "get_sql_connection", open_connection)
    monkeypatch.setattr(attempt_module, "create_stage_state", create_state)
    slices = None
    if keyed:
        slices = [
            keys_module.build_transfer_slice(
                index=0,
                source_sql="SELECT id FROM source WHERE {id}",
                transfer_keys=[keys_module.TransferKey("id", "id")],
                values=(1,),
            )
        ]
    options = make_progress_options(transfer_slices=slices)
    with pytest.raises(ConnectionError) as caught:
        attempt_module.run_transfer_attempt(options, read_retry_cnt=1, insert_retry_cnt=1)
    assert caught.value is failure
    assert source.close_calls == 1
    assert target.close_calls == (failure_at == "state")
