from __future__ import annotations

import pytest
from analytics_toolkit import sql

from tests.sql._support.stage_cleanup import (
    InvalidTextRepresentationError,
    patch_stale_cleanup,
)


def test_stale_stage_cleanup_stops_before_reconnect_and_preserves_permanent_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = InvalidTextRepresentationError("invalid integer input")
    probe = patch_stale_cleanup(monkeypatch, [error], rollback_failure=True)

    with pytest.raises(InvalidTextRepresentationError) as caught:
        sql.cleanup_stale_stage_tables(
            "warehouse",
            stage_tables=["scratch.failed_stage"],
            read_retry_cnt=4,
            timeout_increment=0,
        )

    assert caught.value is error
    assert getattr(error, "analytics_toolkit_sql_backend", None) == "gp"
    assert getattr(error, "analytics_toolkit_sql_retry_safe", True) is False
    assert probe.events == ["drop", "rollback"]
    assert probe.close_calls == 1


def test_stale_stage_cleanup_acquisition_error_skips_rollback_and_reconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = InvalidTextRepresentationError("invalid connection startup setting")
    probe = patch_stale_cleanup(monkeypatch, [], acquisition_failure=error)

    with pytest.raises(InvalidTextRepresentationError) as caught:
        sql.cleanup_stale_stage_tables(
            "warehouse",
            stage_tables=["scratch.failed_stage"],
            read_retry_cnt=3,
            timeout_increment=0,
        )

    assert caught.value is error
    assert probe.events == ["acquire"]
    assert getattr(error, "analytics_toolkit_sql_retry_safe", True) is False
    assert probe.close_calls == 1


@pytest.mark.parametrize("failures", [1, 3])
def test_stale_stage_cleanup_reconnects_only_when_another_attempt_starts(
    monkeypatch: pytest.MonkeyPatch,
    failures: int,
) -> None:
    error = RuntimeError("temporary table drop failure")
    probe = patch_stale_cleanup(monkeypatch, [error] * failures)

    if failures == 1:
        sql.cleanup_stale_stage_tables(
            "warehouse",
            stage_tables=["scratch.failed_stage"],
            read_retry_cnt=3,
            timeout_increment=0,
        )
        assert probe.events == ["drop", "rollback", "reconnect", "drop"]
    else:
        with pytest.raises(RuntimeError) as caught:
            sql.cleanup_stale_stage_tables(
                "warehouse",
                stage_tables=["scratch.failed_stage"],
                read_retry_cnt=3,
                timeout_increment=0,
            )
        assert caught.value is error
        assert probe.events == ["drop", "rollback", "reconnect"] * 2 + ["drop", "rollback"]
    assert probe.close_calls == 1
