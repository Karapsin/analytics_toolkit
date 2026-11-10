from __future__ import annotations

import pytest

from tests.sql._support.facade_cleanup import arrange_facade_cleanup, invoke_cleanup_facade


@pytest.mark.parametrize("operation", ["table_info", "column_names", "drop_tables", "gp_vacuum"])
@pytest.mark.parametrize("outcome", ["success", "failure", "interrupt"])
def test_facade_preserves_established_outcome_when_connection_close_fails(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    outcome: str,
) -> None:
    if outcome == "failure":
        primary_error: BaseException | None = PermissionError("operation denied")
    elif outcome == "interrupt":
        primary_error = KeyboardInterrupt()
    else:
        primary_error = None
    connection, calls = arrange_facade_cleanup(monkeypatch, operation, primary_error)

    if primary_error is not None:
        with pytest.raises(type(primary_error)) as caught:
            invoke_cleanup_facade(operation)
        assert caught.value is primary_error
        assert "SQL close_connection also failed: ConnectionError" in getattr(
            primary_error, "__notes__", []
        )
    else:
        result = invoke_cleanup_facade(operation)
        if operation == "table_info":
            assert result.columns == {"id": "BIGINT"}
            assert result.row_count == 42
        elif operation == "column_names":
            assert result == ("id",)
        elif operation == "drop_tables":
            assert result.metadata.operation_status == "success"
        else:
            assert result is None

    assert calls == [operation]
    assert connection.close_calls == 1
