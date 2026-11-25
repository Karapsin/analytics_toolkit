from __future__ import annotations

import importlib
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from tests.sql._support.lifecycle import maintenance
from tests.sql._support.row_batches import staging_module

load_stage_module = importlib.import_module("analytics_toolkit.sql.dml.load.stage")

if TYPE_CHECKING:
    import pytest


class InvalidTextRepresentationError(Exception):
    pgcode = "22P02"
    analytics_toolkit_sql_backend: str
    analytics_toolkit_sql_retry_safe: bool


class CleanupProbe:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


def patch_stale_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    failures: list[Exception],
    *,
    rollback_failure: bool = False,
    acquisition_failure: Exception | None = None,
) -> CleanupProbe:
    probe = CleanupProbe()
    remaining = list(failures)

    def drop(*_args: Any, **_kwargs: Any) -> None:
        probe.events.append("drop")
        if remaining:
            raise remaining.pop(0)

    def rollback(_connection: Any) -> None:
        probe.events.append("rollback")
        if rollback_failure:
            message = "rollback connection lost"
            raise OSError(message)

    def reconnect(_key: str, _ref: dict[str, Any]) -> None:
        probe.events.append("reconnect")

    monkeypatch.setattr(staging_module, "get_sql_connection", lambda _key: probe)
    monkeypatch.setattr(
        staging_module,
        "get_connection_config",
        lambda key: SimpleNamespace(
            connection_key=key,
            backend="gp",
            transfer_staging_schema="scratch",
            user="test_user",
        ),
    )
    monkeypatch.setattr(staging_module, "replace_connection", reconnect)
    monkeypatch.setattr(maintenance, "drop_table", drop)
    monkeypatch.setattr(
        maintenance,
        "get_backend_adapter",
        lambda _backend: SimpleNamespace(rollback_quietly=rollback),
    )
    if acquisition_failure is not None:

        def acquire(_key: str, _ref: dict[str, Any]) -> Any:
            probe.events.append("acquire")
            raise acquisition_failure

        monkeypatch.setattr(maintenance, "ensure_connection_ref", acquire)
    return probe
