"""Data checkpoint retention does not prohibit replay or weaken structural holds."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from analytics_toolkit.sql.connection.errors import InvalidSqlInputError
from analytics_toolkit.sql.dml.transfer.runtime import retry
from analytics_toolkit.sql.dml.transfer.runtime.retry import (
    is_non_retryable_sql_error,
    run_with_retry,
)
from analytics_toolkit.sql.execution.backend_options import (
    backend_operation,
    clear_data_replay_retention,
    preserve_recovery_artifacts,
)
from analytics_toolkit.sql.execution.plans import SqlOperationMetadata


def test_certified_data_success_releases_retention_but_uncertain_structure_does_not() -> None:
    clear_data_replay_retention()
    assert preserve_recovery_artifacts() is False
    with backend_operation() as state:
        state.live_submitted = True
        state.preserve_recovery = True
        assert is_non_retryable_sql_error(ConnectionError("lost acknowledgement")) is False
        clear_data_replay_retention()
        assert state.preserve_recovery is False
        state.structural_replay_blocked = True
        state.preserve_recovery = True
        clear_data_replay_retention()
        assert state.preserve_recovery is True
        assert is_non_retryable_sql_error(ConnectionError("lost acknowledgement")) is True


def test_data_replay_metadata_is_attached_without_requiring_cluster_status() -> None:
    with backend_operation() as state:
        state.live_submitted = True

        def operation(attempt: int) -> int:
            if attempt == 1:
                message = "lost acknowledgement"
                raise ConnectionError(message)
            return 42

        assert run_with_retry("data replay", 2, 0, operation) == 42
        result = SimpleNamespace(metadata=SqlOperationMetadata())
        state.attach(result)
        assert result.metadata.data_write_replay_possible is True
        assert result.metadata.ch_cluster_status is None


def test_invalid_sql_inputs_do_not_retry() -> None:
    attempts: list[int] = []

    def operation(attempt: int) -> None:
        attempts.append(attempt)
        message = "deterministic target validation"
        raise InvalidSqlInputError(message)

    with pytest.raises(InvalidSqlInputError):
        run_with_retry("invalid input", 3, 0, operation)
    assert attempts == [1]


def test_cleanup_preserves_error_when_exception_has_no_callable_note_hook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = RuntimeError("worker failed")
    original.__dict__["add_note"] = None

    def close(_ref: object, _key: str, _role: str) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(retry, "close_connection_ref", close)
    retry.close_connection_refs_preserving(original, ({"connection": object()}, "gp", "target"))
    assert original.__dict__["analytics_toolkit_sql_retry_safe"] is False
