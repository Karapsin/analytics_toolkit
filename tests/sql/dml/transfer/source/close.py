"""A failed source cursor close cannot fail or replay a completed public transfer."""

from __future__ import annotations

import pytest

from tests.sql._support.dbapi_source_close import SourceFailure, install_source_close


def test_public_transfer_source_cursor_close_failure_keeps_success_and_does_not_replay(monkeypatch):
    scenario, state = install_source_close(monkeypatch, "success")

    result = scenario.run(metadata=True)

    assert result.rows == 2
    assert scenario.loaded == [[(7,), (8,)]]
    assert scenario.read_count == scenario.source_open_count == 1
    assert scenario.finalizations == 1
    assert len(state.queries) == 1
    assert all(
        cursor.close_calls == 1 for connection in state.opened for cursor in connection.cursors
    )
    assert all(connection.close_calls == 1 for connection in state.opened)
    assert any("Cursor close failed" in message for message in state.messages)
    assert all("private cursor-close diagnostic" not in message for message in state.messages)


@pytest.mark.parametrize("phase", ["execute", "fetch"])
def test_public_transfer_source_cursor_close_keeps_primary_failure_and_resource_accounting(
    monkeypatch, phase
):
    scenario, state = install_source_close(monkeypatch, phase)

    with pytest.raises(SourceFailure, match=f"primary {phase} failure") as caught:
        scenario.run(metadata=True)

    assert caught.value is state.failure
    assert scenario.finalizations == 0
    assert len(state.queries) == 1
    assert all(
        cursor.close_calls == 1 for connection in state.opened for cursor in connection.cursors
    )
    assert all(connection.close_calls == 1 for connection in state.opened)
    assert all(connection.rollback_calls == 0 for connection in state.opened)
    assert len(state.opened) == 1
    assert any("Cursor close failed" in message for message in state.messages)
    assert all("private cursor-close diagnostic" not in message for message in state.messages)
