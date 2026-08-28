from __future__ import annotations

# ruff: noqa: EM101, TRY003
import importlib
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pytest
from analytics_toolkit.sql.backends.ch.safe_replace import (
    _cleanup_orphan_replacements,
)
from analytics_toolkit.sql.backends.models import StageFinalizationRequest
from analytics_toolkit.sql.backends.safe_replace import (
    _reversible_cutover,
    finalize_existing_stage_replace,
)
from analytics_toolkit.sql.connection.errors import AmbiguousSqlReplaceError

ch_safe_replace = importlib.import_module("analytics_toolkit.sql.backends.ch.safe_replace")


class _Cursor:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection

    def execute(self, sql: str) -> None:
        self.connection.executed.append(sql)
        if self.connection.fail_execute_at == len(self.connection.executed):
            raise OSError("cutover failed")

    def close(self) -> None:
        return


class _Connection:
    def __init__(
        self,
        *,
        fail_commit: bool = False,
        fail_execute_at: int | None = None,
    ) -> None:
        self.executed: list[str] = []
        self.fail_commit = fail_commit
        self.commit_calls = 0
        self.rollback_calls = 0
        self.fail_execute_at = fail_execute_at

    def cursor(self) -> _Cursor:
        return _Cursor(self)

    def commit(self) -> None:
        self.commit_calls += 1
        if self.fail_commit:
            raise OSError("connection lost during commit")

    def rollback(self) -> None:
        self.rollback_calls += 1


class _Adapter:
    backend = "gp"
    sqlglot_dialect = "postgres"
    supports_transactions = True

    def __init__(self) -> None:
        self.created: list[str] = []
        self.inserted: list[tuple[str, str]] = []
        self.dropped: list[str] = []
        self.commands: list[str] = []

    def ensure_stage_target_table(self, request: Any) -> None:
        self.created.append(request.target_table)

    def insert_from_table(
        self,
        _connection: Any,
        target: str,
        source: str,
        **_kwargs: Any,
    ) -> None:
        self.inserted.append((target, source))

    def count_table_rows(self, _connection: Any, _table: str) -> int:
        return 2

    def quote_identifier(self, value: str) -> str:
        return f'"{value}"'

    def drop_table_sql(self, table: str, **_kwargs: Any) -> str:
        return f"DROP TABLE IF EXISTS {table}"

    def drop_table(self, _connection: Any, table: str, **_kwargs: Any) -> None:
        self.dropped.append(table)

    def execute_command(self, _connection: Any, sql: str) -> None:
        self.commands.append(sql)


def _request(connection: Any) -> StageFinalizationRequest:
    return StageFinalizationRequest(
        connection=connection,
        stage_table="stage.incoming",
        target_table="sandbox.target",
        replace_target_table=True,
        target_exists=True,
        sample_batch=pd.DataFrame({"id": [1, 2], "new_column": ["a", "b"]}),
        target_column_types={"id": "BIGINT", "new_column": "TEXT"},
        insert_column_types={"id": "BIGINT", "new_column": "TEXT"},
    )


def test_greenplum_replace_builds_offside_table_before_transactional_cutover() -> None:
    connection = _Connection()
    adapter = _Adapter()

    assert finalize_existing_stage_replace(adapter, _request(connection)) is True

    replacement = adapter.created[0]
    assert replacement.startswith("sandbox.target__replace_")
    assert adapter.inserted == [(replacement, "stage.incoming")]
    assert connection.executed[0].startswith(
        'ALTER TABLE sandbox.target RENAME TO "target__backup_'
    )
    assert connection.executed[1] == f'ALTER TABLE {replacement} RENAME TO "target"'
    assert connection.executed[2].startswith("DROP TABLE IF EXISTS sandbox.target__backup_")
    assert connection.commit_calls == 1
    assert adapter.dropped == []


def test_greenplum_ambiguous_commit_does_not_replay_or_drop_recovery_artifacts() -> None:
    connection = _Connection(fail_commit=True)
    adapter = _Adapter()

    with pytest.raises(AmbiguousSqlReplaceError, match="commit failed"):
        finalize_existing_stage_replace(adapter, _request(connection))

    assert connection.commit_calls == 1
    assert adapter.dropped == []


def test_replace_helper_ignores_other_backends_and_cleans_count_mismatch() -> None:
    adapter = _Adapter()
    adapter.backend = "ch"
    assert finalize_existing_stage_replace(adapter, _request(_Connection())) is False

    adapter.backend = "gp"
    adapter.count_table_rows = lambda _connection, table: 1 if "replace" in table else 2
    with pytest.raises(RuntimeError, match="Replacement row count"):
        finalize_existing_stage_replace(adapter, _request(_Connection()))
    assert adapter.dropped
    assert "__replace_" in adapter.dropped[0]


def test_transactional_cutover_rolls_back_and_cleans_replacement() -> None:
    connection = _Connection(fail_execute_at=2)
    adapter = _Adapter()

    with pytest.raises(OSError, match="cutover failed"):
        finalize_existing_stage_replace(adapter, _request(connection))

    assert connection.rollback_calls == 1
    assert len(adapter.dropped) == 1


def test_trino_reversible_cutover_succeeds_when_backup_cleanup_fails() -> None:
    adapter = _Adapter()
    adapter.backend = "trino"
    adapter.sqlglot_dialect = "trino"
    adapter.supports_transactions = False

    def fail_drop(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("cleanup failed")

    adapter.drop_table = fail_drop
    assert finalize_existing_stage_replace(adapter, _request(_Connection())) is True
    assert len(adapter.commands) == 2
    assert adapter.commands[0].startswith("ALTER TABLE sandbox.target RENAME TO sandbox.")


def test_trino_reversible_cutover_rolls_back_bad_final_count() -> None:
    adapter = _Adapter()
    adapter.backend = "trino"
    adapter.sqlglot_dialect = "trino"
    adapter.supports_transactions = False

    def count_rows(_connection: Any, table: str) -> int:
        return 1 if table == "sandbox.target" and adapter.commands else 2

    adapter.count_table_rows = count_rows
    with pytest.raises(RuntimeError, match="Final replacement row count"):
        finalize_existing_stage_replace(adapter, _request(_Connection()))
    assert len(adapter.commands) == 4


def test_trino_reversible_cutover_handles_failure_before_first_rename() -> None:
    adapter = _Adapter()
    adapter.backend = "trino"
    adapter.supports_transactions = False

    def fail_command(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("rename failed")

    adapter.execute_command = fail_command
    with pytest.raises(OSError, match="rename failed"):
        _reversible_cutover(
            adapter,
            _request(_Connection()),
            "sandbox.replacement",
            "sandbox.backup",
            2,
        )


def test_trino_reversible_cutover_reports_ambiguous_failed_rollback() -> None:
    adapter = _Adapter()
    adapter.backend = "trino"
    adapter.sqlglot_dialect = "trino"
    adapter.supports_transactions = False

    def count_rows(_connection: Any, table: str) -> int:
        return 1 if table == "sandbox.target" and adapter.commands else 2

    def execute_command(_connection: Any, sql: str) -> None:
        adapter.commands.append(sql)
        if len(adapter.commands) == 3:
            raise OSError("rollback failed")

    adapter.count_table_rows = count_rows
    adapter.execute_command = execute_command
    with pytest.raises(AmbiguousSqlReplaceError, match="automatic rollback"):
        finalize_existing_stage_replace(adapter, _request(_Connection()))


def test_replace_artifact_cleanup_failure_preserves_original_error() -> None:
    adapter = _Adapter()

    def fail_insert(*_args: Any, **_kwargs: Any) -> None:
        raise ValueError("insert failed")

    def fail_drop(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("drop failed")

    adapter.insert_from_table = fail_insert
    adapter.drop_table = fail_drop
    with pytest.raises(ValueError, match="insert failed"):
        finalize_existing_stage_replace(adapter, _request(_Connection()))


class _ChConnection:
    def __init__(self) -> None:
        self.queries: list[str] = []
        self.commands: list[str] = []

    def query(self, sql: str) -> Any:
        self.queries.append(sql)
        if "system.processes" in sql or "system, processes" in sql:
            return SimpleNamespace(result_rows=[(0,)])
        return SimpleNamespace(result_rows=[("target__replace_abcd",)])

    def command(self, sql: str, **_kwargs: Any) -> None:
        self.commands.append(sql)


class _ChAdapter:
    def __init__(self) -> None:
        self.commands: list[str] = []

    def execute_command(self, _connection: Any, sql: str) -> None:
        self.commands.append(sql)


class _ReplaceChAdapter(_ChAdapter):
    def __init__(self, counts: list[int]) -> None:
        super().__init__()
        self.counts = counts
        self.created: list[str] = []
        self.inserted: list[tuple[str, str]] = []

    def companion_table_name(self, table: str) -> str:
        return f"{table}_shard"

    def ensure_stage_target_table(self, request: Any) -> None:
        self.created.append(request.target_table)

    def insert_from_table(
        self,
        _connection: Any,
        target: str,
        source: str,
        **_kwargs: Any,
    ) -> None:
        self.inserted.append((target, source))

    def count_table_rows(self, _connection: Any, _table: str) -> int:
        return self.counts.pop(0)


def test_clickhouse_orphan_cleanup_drops_only_inactive_replacement_tables() -> None:
    connection = _ChConnection()
    adapter = _ChAdapter()

    _cleanup_orphan_replacements(
        adapter,
        connection,
        [("sandbox.target", "core")],
    )

    assert adapter.commands == ["DROP TABLE IF EXISTS sandbox.target__replace_abcd ON CLUSTER core"]


def test_clickhouse_orphan_cleanup_keeps_active_and_ignores_bad_or_failed_metadata() -> None:
    adapter = _ChAdapter()

    class ActiveConnection(_ChConnection):
        def query(self, sql: str) -> Any:
            if "processes" in sql:
                return SimpleNamespace(result_rows=[(1,)])
            return SimpleNamespace(
                result_rows=[(), ("not-a-replacement",), ("target__replace_live",)]
            )

    _cleanup_orphan_replacements(
        adapter,
        ActiveConnection(),
        [("sandbox.target", None)],
    )
    assert adapter.commands == []

    class BrokenConnection:
        def query(self, _sql: str) -> Any:
            raise OSError("metadata unavailable")

    _cleanup_orphan_replacements(
        adapter,
        BrokenConnection(),
        [("sandbox.target", None)],
    )


def test_clickhouse_replace_builds_and_swaps_complete_distributed_pair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _ChConnection()
    adapter = _ReplaceChAdapter([2, 2, 2])
    monkeypatch.setattr(ch_safe_replace, "_cleanup_orphan_replacements", lambda *_args: None)
    monkeypatch.setattr(ch_safe_replace, "_database_engine_local", lambda *_args: "Atomic")

    ch_safe_replace._replace_from_stage(adapter, _request(connection))

    assert adapter.created[0].startswith("sandbox.target_shard__replace_")
    assert adapter.inserted[0][1] == "stage.incoming"
    assert any(
        command.startswith("CREATE TABLE sandbox.target__replace_") for command in adapter.commands
    )
    assert any("EXCHANGE TABLES" in command for command in connection.commands)
    assert any("__replace_" in command and "DROP TABLE" in command for command in adapter.commands)


def test_clickhouse_replace_rolls_back_failed_final_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _ChConnection()
    adapter = _ReplaceChAdapter([2, 2, 1])
    monkeypatch.setattr(ch_safe_replace, "_cleanup_orphan_replacements", lambda *_args: None)
    monkeypatch.setattr(ch_safe_replace, "_database_engine_local", lambda *_args: "Atomic")

    with pytest.raises(RuntimeError, match="final target row count"):
        ch_safe_replace._replace_from_stage(adapter, _request(connection))

    assert sum("EXCHANGE TABLES" in command for command in connection.commands) >= 4


def test_clickhouse_only_shard_replace_skips_distributed_facade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _ChConnection()
    adapter = _ReplaceChAdapter([2, 2, 2])
    request = _request(connection)
    request = StageFinalizationRequest(**{**request.__dict__, "ch_only_shard": True})
    monkeypatch.setattr(ch_safe_replace, "_cleanup_orphan_replacements", lambda *_args: None)
    monkeypatch.setattr(ch_safe_replace, "_database_engine_local", lambda *_args: "Atomic")

    ch_safe_replace._replace_from_stage(adapter, request)

    assert adapter.created[0].startswith("sandbox.target__replace_")
    assert not any(command.startswith("CREATE TABLE") for command in adapter.commands)


def test_clickhouse_replace_reports_ambiguous_failed_rollback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _ChConnection()
    adapter = _ReplaceChAdapter([2, 2, 1])
    calls = 0

    def execute_sqls(current_adapter: Any, current_connection: Any, sqls: list[str]) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("rollback unavailable")
        for sql in sqls:
            current_adapter.execute_command(current_connection, sql)

    monkeypatch.setattr(ch_safe_replace, "_cleanup_orphan_replacements", lambda *_args: None)
    monkeypatch.setattr(ch_safe_replace, "_database_engine_local", lambda *_args: "Atomic")
    monkeypatch.setattr(ch_safe_replace, "execute_reconfiguration_sqls", execute_sqls)

    with pytest.raises(AmbiguousSqlReplaceError, match="automatic rollback"):
        ch_safe_replace._replace_from_stage(adapter, _request(connection))


@pytest.mark.parametrize(
    ("counts", "fingerprints", "message"),
    [
        ([2, 1], [("same",), ("same",)], "replacement row count"),
        ([2, 2], [("old",), ("new",)], "destination changed"),
    ],
)
def test_clickhouse_replace_rejects_incomplete_or_stale_replacement(
    monkeypatch: pytest.MonkeyPatch,
    counts: list[int],
    fingerprints: list[tuple[str]],
    message: str,
) -> None:
    adapter = _ReplaceChAdapter(counts)
    monkeypatch.setattr(ch_safe_replace, "_cleanup_orphan_replacements", lambda *_args: None)
    monkeypatch.setattr(ch_safe_replace, "_database_engine_local", lambda *_args: "Atomic")
    monkeypatch.setattr(
        ch_safe_replace,
        "_target_fingerprint",
        lambda *_args: fingerprints.pop(0),
    )

    with pytest.raises(RuntimeError, match=message):
        ch_safe_replace._replace_from_stage(adapter, _request(_ChConnection()))


def test_clickhouse_replace_requires_a_physical_target() -> None:
    adapter = _ReplaceChAdapter([])
    adapter.companion_table_name = lambda _table: None

    with pytest.raises(RuntimeError, match="physical shard"):
        ch_safe_replace._replace_from_stage(adapter, _request(_ChConnection()))


def test_clickhouse_replace_helpers_cover_local_clusters_and_cleanup_failures() -> None:
    request = _request(_ChConnection())
    assert ch_safe_replace._clusters(request) == ("{cluster}", "{cluster}", "{cluster}")
    request = StageFinalizationRequest(
        **{
            **request.__dict__,
            "ch_creation_policy": SimpleNamespace(
                shard_on_cluster="shards",
                distributed_on_cluster="facades",
                distributed_cluster="reads",
            ),
        }
    )
    assert ch_safe_replace._clusters(request) == ("shards", "facades", "reads")
    assert ch_safe_replace._target_fingerprint(_ChConnection(), "sandbox.target", None)

    class _FailingAdapter:
        def execute_command(self, *_args: Any) -> None:
            raise OSError("drop unavailable")

    ch_safe_replace._best_effort_drop(
        _FailingAdapter(),
        _ChConnection(),
        [(None, None), ("sandbox.target", None)],
    )
