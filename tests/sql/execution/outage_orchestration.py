"""Independent orchestration tasks inherit policy without sharing outage state."""

from __future__ import annotations

import importlib
from threading import Event
from typing import Any

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.dml.transfer.runtime.retry import is_non_retryable_sql_error
from analytics_toolkit.sql.execution.backend_options import (
    backend_operation,
    current_backend_operation,
    preserve_recovery_artifacts,
    public_backend_options,
    require_complete_operation,
)
from analytics_toolkit.sql.execution.plans import SqlOperationMetadata, SqlOperationResult


@pytest.mark.parametrize("runner", ["parallel_sql", "async_sql"])
@pytest.mark.parametrize("policy", [None, "stop", "healthy_shards"])
def test_outage_orchestration_isolates_sibling_retries_cleanup_and_metadata(
    monkeypatch: Any, runner: str, policy: str | None
) -> None:
    module = importlib.import_module(f"analytics_toolkit.sql.orchestration.{runner}")
    written = Event()
    states = {}

    @public_backend_options
    def execute_sql(*, db_key: str, query: str, progress: bool = False) -> Any:
        del query, progress
        state = current_backend_operation()
        assert state is not None
        states[db_key] = state
        if db_key == "ch":
            state.live_submitted = True
            state.observed_partial = True
            state.statuses[("ch", "core")] = {"alias": "ch", "cluster": "core", "complete": False}
            assert preserve_recovery_artifacts(failed=True)
            written.set()
        else:
            assert written.wait(5)
            assert not is_non_retryable_sql_error(ConnectionError("temporary GP disconnect"))
            assert not preserve_recovery_artifacts()
            require_complete_operation("independent GP replacement must remain allowed")
        return SqlOperationResult(data=db_key, rows=None, metadata=SqlOperationMetadata())

    monkeypatch.setattr(module, "execute_sql", execute_sql)
    tasks = [
        {"name": "write", "type": "execute", "db_key": "ch", "query": "INSERT"},
        {"name": "other", "type": "execute", "db_key": "gp", "query": "REPLACE"},
    ]
    with backend_operation("stop") as parent:
        result = getattr(sql, runner)(tasks, concurrency=2, ch_unavailable_shards=policy)
        assert current_backend_operation() is parent
        assert not parent.live_submitted
        assert not parent.partial
    assert states["ch"] is not states["gp"]
    assert states["ch"].ch_unavailable_shards == (policy or "stop")
    assert states["gp"].ch_unavailable_shards == (policy or "stop")
    assert result["write"].metadata.data_complete is False
    assert result["other"].metadata.data_complete is None
    assert not result["other"].metadata.ch_cluster_status


@pytest.mark.parametrize("runner", ["parallel_sql", "async_sql"])
def test_outage_orchestration_inherits_defaults_and_allows_per_task_override(
    monkeypatch: Any, runner: str
) -> None:
    module = importlib.import_module(f"analytics_toolkit.sql.orchestration.{runner}")

    @public_backend_options
    def read_sql(**_kwargs: Any) -> Any:
        return current_backend_operation().ch_unavailable_shards

    monkeypatch.setattr(module, "read_sql", read_sql)
    tasks = [
        {"name": "default", "type": "read", "db_key": "ch", "query": "SELECT 1"},
        {
            "name": "override",
            "type": "read",
            "db_key": "ch",
            "query": "SELECT 1",
            "ch_unavailable_shards": "stop",
        },
    ]
    assert getattr(sql, runner)(tasks, ch_unavailable_shards="healthy_shards") == {
        "default": "healthy_shards",
        "override": "stop",
    }
    assert getattr(sql, runner)(tasks)["default"] == "success"
    monkeypatch.setattr(module, "execute_sql", lambda **_kwargs: getattr(sql, runner)(tasks))
    nested = [{"name": "nested", "type": "execute", "db_key": "ch", "query": "SELECT 1"}]
    assert getattr(sql, runner)(nested, ch_unavailable_shards="healthy_shards")["nested"] == {
        "default": "healthy_shards",
        "override": "stop",
    }
    with pytest.raises(ValueError, match="ch_unavailable_shards"):
        getattr(sql, runner)(tasks, ch_unavailable_shards="invalid")
