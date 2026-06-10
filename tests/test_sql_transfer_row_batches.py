from __future__ import annotations

import importlib
import warnings
import sys
from pathlib import Path
from typing import Any
from types import SimpleNamespace

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

attempt_module = importlib.import_module(
    "analytics_toolkit.sql.dml.transfer.flow.attempt"
)
finalize_module = importlib.import_module(
    "analytics_toolkit.sql.dml.transfer.flow.finalize"
)
estimate_module = importlib.import_module(
    "analytics_toolkit.sql.dml.transfer.flow.estimate"
)
staging_module = importlib.import_module(
    "analytics_toolkit.sql.dml.transfer.staging"
)
transfer_api_module = importlib.import_module(
    "analytics_toolkit.sql.dml.transfer.flow.api"
)
models_module = importlib.import_module(
    "analytics_toolkit.sql.dml.transfer.runtime.models"
)


class RecordingSourceCursor:
    def __init__(self, rows: list[tuple[int]]) -> None:
        self._rows = rows
        self.description = [("id", 23, None, None, None, None)]
        self.fetch_sizes: list[int] = []
        self.executed: list[str] = []
        self.close_calls = 0

    def execute(self, query: str) -> None:
        self.executed.append(query)

    def fetchmany(self, size: int) -> list[tuple[int]]:
        self.fetch_sizes.append(size)
        batch = self._rows[:size]
        self._rows = self._rows[size:]
        return batch

    def close(self) -> None:
        self.close_calls += 1


class RecordingSourceConnection:
    def __init__(self, rows: list[tuple[int]]) -> None:
        self.cursor_obj = RecordingSourceCursor(rows)

    def cursor(self) -> RecordingSourceCursor:
        return self.cursor_obj


class StaticDbapiCursor:
    def __init__(
        self,
        connection: StaticDbapiConnection,
        rows: list[tuple[Any, ...]],
    ) -> None:
        self.connection = connection
        self._rows = rows
        self.close_calls = 0

    def execute(self, query: str) -> None:
        self.connection.executed.append(query)

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)

    def close(self) -> None:
        self.close_calls += 1


class StaticDbapiConnection:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.rows = rows
        self.executed: list[str] = []
        self.rollback_calls = 0

    def cursor(self) -> StaticDbapiCursor:
        return StaticDbapiCursor(self, self.rows)

    def rollback(self) -> None:
        self.rollback_calls += 1


class StaticClickHouseResult:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.result_rows = rows


class StaticClickHouseClient:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.rows = rows
        self.queries: list[str] = []

    def query(self, query: str) -> StaticClickHouseResult:
        self.queries.append(query)
        return StaticClickHouseResult(self.rows)


class FakeTransferConnection:
    def __init__(self, name: str) -> None:
        self.name = name
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


class RenderingFakeTqdm:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.total = kwargs["total"]
        self.n = 0
        self.rendered: list[str] = []

    @property
    def format_dict(self) -> dict[str, Any]:
        desc = self.kwargs["desc"]
        return {
            "n": self.n,
            "total": self.total,
            "desc": desc,
            "unit": self.kwargs["unit"],
            "elapsed": "00:00",
            "remaining": "00:02",
            "rate_fmt": "14087.46row/s",
            "postfix": "",
            "l_bar": f"{desc}:  86%|",
            "bar": "########",
        }

    def update(self, value: int) -> None:
        self.n += value
        if not self.kwargs["disable"]:
            self.rendered.append(self.kwargs["bar_format"].format(**self.format_dict))


def capture_rendering_progress_bars(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    progress_bars: list[Any] = []

    class CapturingTqdm(RenderingFakeTqdm):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            progress_bars.append(self)

    monkeypatch.setattr(attempt_module, "tqdm", CapturingTqdm)
    return progress_bars


def make_progress_options(**overrides: Any) -> Any:
    values = {
        "from_db_key": "gp",
        "from_db_backend": "gp",
        "to_db_key": "gp_sandbox",
        "to_db_backend": "gp",
        "source_sql": "select id from source_table",
        "target_table": "sandbox.target",
        "batch_size": 2,
        "progress": True,
    }
    values.update(overrides)
    return models_module.TransferOptions(**values)


def test_run_transfer_attempt_cleans_staging_schema_on_start_and_finish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    cleanup_calls: list[tuple[str, int]] = []
    source_conn = FakeTransferConnection("source")
    target_conn = FakeTransferConnection("target")

    options = make_progress_options(
        transfer_staging_schema="transfer_schema",
        transfer_staging_username="target_user",
        to_db_key="target_db",
        to_db_backend="gp",
        from_db_key="source_db",
        from_db_backend="gp",
    )

    def fake_get_sql_connection(connection_key: str) -> FakeTransferConnection:
        if connection_key == "source_db":
            return source_conn
        if connection_key == "target_db":
            return target_conn
        raise AssertionError(f"unexpected connection key: {connection_key}")

    def fake_create_stage_state(*_args: Any, **_kwargs: Any) -> models_module.TransferStageState:
        events.append("create_stage_state")
        return models_module.TransferStageState(target_exists=False)

    def fake_cleanup_stale_stage_tables_with_connection(
        db_key: str,
        target_table: str,
        connection_ref: dict[str, Any],
        read_retry_cnt: int,
        stage_tables: list[str] | None = None,
        timeout_increment: int | float = 5,
        query_label: str | None = None,
    ) -> None:
        del db_key, target_table, stage_tables, timeout_increment, query_label
        events.append(
            "cleanup_start"
            if not cleanup_calls
            else "cleanup_finish"
        )
        cleanup_calls.append((connection_ref["connection"].name, read_retry_cnt))

    def fake_inspect_source_query_schema(*_args: Any, **_kwargs: Any) -> list[Any]:
        events.append("inspect_source_query_schema")
        return []

    def fake_load_stage_batches(*_args: Any, **_kwargs: Any) -> int:
        events.append("load_stage_batches")
        return 7

    def fake_finalize_loaded_stage(*_args: Any, **_kwargs: Any) -> None:
        events.append("finalize_loaded_stage")

    def fake_cleanup_stage(*_args: Any, **_kwargs: Any) -> None:
        events.append("cleanup_stage")

    def fake_close_connection_ref(
        _connection_ref: dict[str, Any],
        _connection_type: str,
        role: str,
    ) -> None:
        events.append(f"close:{role}")

    monkeypatch.setattr(attempt_module, "get_sql_connection", fake_get_sql_connection)
    monkeypatch.setattr(attempt_module, "create_stage_state", fake_create_stage_state)
    monkeypatch.setattr(
        attempt_module,
        "cleanup_stale_stage_tables_with_connection",
        fake_cleanup_stale_stage_tables_with_connection,
    )
    monkeypatch.setattr(
        attempt_module,
        "inspect_source_query_schema",
        fake_inspect_source_query_schema,
    )
    monkeypatch.setattr(attempt_module, "load_stage_batches", fake_load_stage_batches)
    monkeypatch.setattr(
        attempt_module,
        "finalize_loaded_stage",
        fake_finalize_loaded_stage,
    )
    monkeypatch.setattr(attempt_module, "cleanup_stage", fake_cleanup_stage)
    monkeypatch.setattr(attempt_module, "close_connection_ref", fake_close_connection_ref)

    total_rows = attempt_module.run_transfer_attempt(
        options=options,
        read_retry_cnt=3,
        insert_retry_cnt=2,
    )

    assert total_rows == 7
    assert cleanup_calls == [("target", 3), ("target", 3)]
    assert events == [
        "create_stage_state",
        "cleanup_start",
        "inspect_source_query_schema",
        "load_stage_batches",
        "finalize_loaded_stage",
        "cleanup_stage",
        "cleanup_finish",
        "close:source",
        "close:target",
    ]


def test_run_transfer_attempt_calls_cleanup_when_staging_schema_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    source_conn = FakeTransferConnection("source")
    target_conn = FakeTransferConnection("target")

    options = make_progress_options(
        to_db_key="target_db",
        to_db_backend="gp",
        from_db_key="source_db",
        from_db_backend="gp",
        transfer_staging_schema=None,
    )

    def fake_get_sql_connection(connection_key: str) -> FakeTransferConnection:
        if connection_key == "source_db":
            return source_conn
        if connection_key == "target_db":
            return target_conn
        raise AssertionError(f"unexpected connection key: {connection_key}")

    def fake_create_stage_state(*_args: Any, **_kwargs: Any) -> models_module.TransferStageState:
        events.append("create_stage_state")
        return models_module.TransferStageState(target_exists=False)

    def fake_inspect_source_query_schema(*_args: Any, **_kwargs: Any) -> list[Any]:
        events.append("inspect_source_query_schema")
        return []

    def fake_load_stage_batches(*_args: Any, **_kwargs: Any) -> int:
        events.append("load_stage_batches")
        return 7

    def fake_finalize_loaded_stage(*_args: Any, **_kwargs: Any) -> None:
        events.append("finalize_loaded_stage")

    def fake_cleanup_stage(*_args: Any, **_kwargs: Any) -> None:
        events.append("cleanup_stage")

    cleanup_calls: list[tuple[str, int]] = []

    def fake_cleanup_stale_stage_tables_with_connection(
        db_key: str,
        target_table: str,
        connection_ref: dict[str, Any],
        read_retry_cnt: int,
        stage_tables: list[str] | None = None,
        timeout_increment: int | float = 5,
        query_label: str | None = None,
    ) -> None:
        del target_table, stage_tables, timeout_increment, query_label
        events.append("cleanup_start" if not cleanup_calls else "cleanup_finish")
        cleanup_calls.append((connection_ref["connection"].name, read_retry_cnt))

    def fake_close_connection_ref(
        _connection_ref: dict[str, Any],
        _connection_type: str,
        role: str,
    ) -> None:
        events.append(f"close:{role}")

    monkeypatch.setattr(attempt_module, "get_sql_connection", fake_get_sql_connection)
    monkeypatch.setattr(attempt_module, "create_stage_state", fake_create_stage_state)
    monkeypatch.setattr(
        attempt_module,
        "inspect_source_query_schema",
        fake_inspect_source_query_schema,
    )
    monkeypatch.setattr(attempt_module, "load_stage_batches", fake_load_stage_batches)
    monkeypatch.setattr(
        attempt_module,
        "finalize_loaded_stage",
        fake_finalize_loaded_stage,
    )
    monkeypatch.setattr(attempt_module, "cleanup_stage", fake_cleanup_stage)
    monkeypatch.setattr(
        attempt_module,
        "cleanup_stale_stage_tables_with_connection",
        fake_cleanup_stale_stage_tables_with_connection,
    )
    monkeypatch.setattr(attempt_module, "close_connection_ref", fake_close_connection_ref)

    total_rows = attempt_module.run_transfer_attempt(
        options=options,
        read_retry_cnt=3,
        insert_retry_cnt=2,
    )

    assert total_rows == 7
    assert events == [
        "create_stage_state",
        "cleanup_start",
        "inspect_source_query_schema",
        "load_stage_batches",
        "finalize_loaded_stage",
        "cleanup_stage",
        "cleanup_finish",
        "close:source",
        "close:target",
    ]
    assert cleanup_calls == [("target", 3), ("target", 3)]


def test_cleanup_stale_stage_tables_warns_once_when_staging_schema_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = SimpleNamespace(
        connection_key="staging_warning_db",
        backend="gp",
        transfer_staging_schema=None,
        user="target_user",
    )
    connection_ref: dict[str, Any] = {"connection": FakeTransferConnection("target")}
    monkeypatch.setattr(staging_module, "get_connection_config", lambda db_key: config)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        staging_module.cleanup_stale_stage_tables_with_connection(
            db_key="staging_warning_db",
            target_table="schema.target",
            connection_ref=connection_ref,
            read_retry_cnt=1,
        )
        staging_module.cleanup_stale_stage_tables_with_connection(
            db_key="staging_warning_db",
            target_table="schema.target",
            connection_ref=connection_ref,
            read_retry_cnt=1,
        )

    assert len(caught) == 1
    assert (
        "clean_transfer_staging_schema is enabled, "
        "but transfer_staging_schema is not configured for the target connection"
        in str(caught[0].message)
    )


def test_cleanup_stale_stage_tables_discovers_matching_gp_stage_tables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    discovered: list[str] = []
    query_calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        staging_module,
        "get_connection_config",
        lambda db_key: SimpleNamespace(
            connection_key=db_key,
            backend="gp",
            transfer_staging_schema="transfer_schema",
            user="target user",
        ),
    )
    monkeypatch.setattr(
        staging_module,
        "_query_gp_stage_tables",
        lambda transfer_staging_schema, table_prefix, connection: query_calls.append(
            (transfer_staging_schema, table_prefix)
        )
        or [
            "target__analytics_toolkit_target_user__stage__match",
            "other__analytics_toolkit_target_user__stage__ignore",
        ],
    )
    monkeypatch.setattr(
        staging_module,
        "cleanup_stage_table_with_retry",
        lambda *args, **kwargs: discovered.append(args[3]),
    )

    staging_module.cleanup_stale_stage_tables_with_connection(
        db_key="gp",
        target_table="analytics.target",
        connection_ref={"connection": object()},
        read_retry_cnt=3,
    )

    assert discovered == ["transfer_schema.target__analytics_toolkit_target_user__stage__match"]
    assert query_calls == [("transfer_schema", "target__analytics_toolkit_target_user__stage__%")]


def test_cleanup_stale_stage_tables_drops_explicit_stage_tables_without_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    discovered: list[str] = []
    query_called = 0

    def fake_query_gp_stage_tables(
        transfer_staging_schema: str,
        table_prefix: str,
        connection: Any,
    ) -> list[str]:
        nonlocal query_called
        query_called += 1
        del transfer_staging_schema, table_prefix, connection
        raise AssertionError("query should not be used for explicit stage tables")

    monkeypatch.setattr(
        staging_module,
        "get_connection_config",
        lambda db_key: SimpleNamespace(
            connection_key=db_key,
            backend="gp",
            transfer_staging_schema="transfer_schema",
            user="target_user",
        ),
    )
    monkeypatch.setattr(staging_module, "_query_gp_stage_tables", fake_query_gp_stage_tables)
    monkeypatch.setattr(
        staging_module,
        "cleanup_stage_table_with_retry",
        lambda *args, **kwargs: discovered.append(args[3]),
    )

    staging_module.cleanup_stale_stage_tables_with_connection(
        db_key="gp",
        target_table="analytics.target",
        connection_ref={"connection": object()},
        read_retry_cnt=3,
        stage_tables=[
            "analytics.target__analytics_toolkit_target_user__stage__explicit",
            "target__analytics_toolkit_target_user__stage__implicit",
        ],
    )

    assert discovered == [
        "analytics.target__analytics_toolkit_target_user__stage__explicit",
        "transfer_schema.target__analytics_toolkit_target_user__stage__implicit",
    ]
    assert query_called == 0


def test_transfer_options_progress_defaults_to_false() -> None:
    options = models_module.TransferOptions(
        from_db_key="gp",
        from_db_backend="gp",
        to_db_key="gp_sandbox",
        to_db_backend="gp",
        source_sql="select id from source_table",
        target_table="sandbox.target",
        batch_size=2,
    )

    assert options.progress is False


def test_adaptive_batch_sizer_grows_shrinks_caps_floors_and_can_disable() -> None:
    sizer = models_module.AdaptiveBatchSizer(
        enabled=True,
        current_size=1_000,
        min_size=500,
        max_size=2_000,
        optimize_by_rows_per_second=False,
        target_seconds=10.0,
    )

    sizer.update(4.9)
    assert sizer.current_size == 1_500
    sizer.update(4.9)
    assert sizer.current_size == 2_000
    sizer.update(10.0)
    assert sizer.current_size == 2_000
    sizer.update(21.0)
    assert sizer.current_size == 1_000
    sizer.update(21.0)
    assert sizer.current_size == 500
    sizer.update(21.0)
    assert sizer.current_size == 500

    disabled = models_module.AdaptiveBatchSizer(
        enabled=False,
        current_size=1_000,
        min_size=500,
        max_size=2_000,
        optimize_by_rows_per_second=False,
        target_seconds=10.0,
    )
    disabled.update(1.0)
    assert disabled.current_size == 1_000


def test_adaptive_batch_sizer_targets_rows_per_second() -> None:
    sizer = models_module.AdaptiveBatchSizer(
        enabled=True,
        current_size=2,
        min_size=1,
        max_size=8,
        target_seconds=10.0,
        optimize_by_rows_per_second=True,
    )

    sizer.update(1.0, inserted_rows=2)
    assert sizer.current_size == 2
    sizer.update(0.5, inserted_rows=2)
    assert sizer.current_size == 3
    sizer.update(2.0, inserted_rows=3)
    assert sizer.current_size == 1
    sizer.update(0.9, inserted_rows=1)
    assert sizer.current_size == 1


def test_adaptive_batch_sizer_targets_rows_per_second_with_smoothing() -> None:
    sizer = models_module.AdaptiveBatchSizer(
        enabled=True,
        current_size=10,
        min_size=1,
        max_size=20,
        target_seconds=10.0,
        optimize_by_rows_per_second=True,
        target_rows_per_second_window=5,
        target_rows_per_second_deadband=0.15,
    )

    sizer.update(1.0, inserted_rows=100)
    sizer.update(1.0, inserted_rows=100)
    sizer.update(1.0, inserted_rows=100)
    sizer.update(1.0, inserted_rows=100)
    assert sizer.current_size == 10

    sizer.update(1.0, inserted_rows=112)
    assert sizer.current_size == 10

    sizer.update(1.0, inserted_rows=200)
    assert sizer.current_size == 15


def test_adaptive_batch_sizer_window_one_preserves_previous_rows_per_second_logic() -> None:
    sizer = models_module.AdaptiveBatchSizer(
        enabled=True,
        current_size=2,
        min_size=1,
        max_size=8,
        target_seconds=10.0,
        optimize_by_rows_per_second=True,
        target_rows_per_second_window=1,
        target_rows_per_second_deadband=0.0,
    )

    sizer.update(1.0, inserted_rows=2)
    assert sizer.current_size == 2
    sizer.update(0.5, inserted_rows=2)
    assert sizer.current_size == 3
    sizer.update(2.0, inserted_rows=3)
    assert sizer.current_size == 1
    sizer.update(0.9, inserted_rows=1)
    assert sizer.current_size == 1


def test_adaptive_batch_sizer_respects_batch_seconds_bounds() -> None:
    sizer = models_module.AdaptiveBatchSizer(
        enabled=True,
        current_size=2,
        min_size=1,
        max_size=8,
        optimize_by_rows_per_second=False,
        target_seconds=10.0,
        min_target_seconds=20.0,
        max_target_seconds=40.0,
    )

    sizer.update(9.9)
    assert sizer.current_size == 3
    sizer.update(10.0)
    assert sizer.current_size == 3
    sizer.update(35.0)
    assert sizer.current_size == 3
    sizer.update(50.0)
    assert sizer.current_size == 1


def test_adaptive_batch_sizer_can_target_memory_instead_of_time() -> None:
    sizer = models_module.AdaptiveBatchSizer(
        enabled=True,
        current_size=100,
        min_size=10,
        max_size=200,
        optimize_by_rows_per_second=False,
        target_seconds=10.0,
        target_memory_bytes=1_000,
    )

    sizer.update(100.0, memory_bytes=400)
    assert sizer.current_size == 150
    sizer.update(1.0, memory_bytes=750)
    assert sizer.current_size == 150
    sizer.update(1.0, memory_bytes=2_000)
    assert sizer.current_size == 75

    unlimited = models_module.AdaptiveBatchSizer(
        enabled=True,
        current_size=100,
        min_size=10,
        max_size=None,
        optimize_by_rows_per_second=False,
        target_seconds=10.0,
        target_memory_bytes=1_000,
    )
    unlimited.update(100.0, memory_bytes=400)
    assert unlimited.current_size == 150
    unlimited.update(100.0, memory_bytes=400)
    assert unlimited.current_size == 225

    no_measurement = models_module.AdaptiveBatchSizer(
        enabled=True,
        current_size=100,
        min_size=10,
        max_size=200,
        optimize_by_rows_per_second=False,
        target_seconds=10.0,
        target_memory_bytes=1_000,
    )
    no_measurement.update(1.0)
    assert no_measurement.current_size == 100

    disabled = models_module.AdaptiveBatchSizer(
        enabled=False,
        current_size=100,
        min_size=10,
        max_size=200,
        optimize_by_rows_per_second=False,
        target_seconds=10.0,
        target_memory_bytes=1_000,
    )
    disabled.update(1.0, memory_bytes=10)
    assert disabled.current_size == 100


def test_adaptive_batch_sizer_respects_batch_memory_bounds() -> None:
    sizer = models_module.AdaptiveBatchSizer(
        enabled=True,
        current_size=100,
        min_size=10,
        max_size=200,
        optimize_by_rows_per_second=False,
        target_seconds=10.0,
        target_memory_bytes=100,
        min_target_memory_bytes=50,
        max_target_memory_bytes=50,
    )
    sizer.update(1.0, memory_bytes=75)
    assert sizer.current_size == 66


def test_transfer_options_resolve_adaptive_bounds_and_validate() -> None:
    options = transfer_api_module.build_transfer_options(
        from_db="gp",
        to_db="trino",
        from_sql="select id from source_table",
        to_table="sandbox.target",
        batch_size=100,
    )

    assert options.min_batch_size == 100
    assert options.max_batch_size == 400
    assert options.target_rows_per_second_window == 5
    assert options.target_rows_per_second_deadband == 0.15

    custom_window_options = transfer_api_module.build_transfer_options(
        from_db="gp",
        to_db="trino",
        from_sql="select id from source_table",
        to_table="sandbox.target",
        target_rows_per_second_window=3,
        target_rows_per_second_deadband=0.05,
    )
    assert custom_window_options.target_rows_per_second_window == 3
    assert custom_window_options.target_rows_per_second_deadband == 0.05

    memory_options = transfer_api_module.build_transfer_options(
        from_db="gp",
        to_db="trino",
        from_sql="select id from source_table",
        to_table="sandbox.target",
        batch_size=100,
        target_batch_memory_mb=64,
    )

    assert memory_options.target_batch_memory_mb == 64.0
    assert memory_options.target_batch_memory_bytes == 64 * 1024 * 1024
    assert memory_options.max_batch_size is None

    capped_memory_options = transfer_api_module.build_transfer_options(
        from_db="gp",
        to_db="trino",
        from_sql="select id from source_table",
        to_table="sandbox.target",
        batch_size=100,
        target_batch_memory_mb=64,
        max_batch_size=1_000,
    )

    assert capped_memory_options.max_batch_size == 1_000

    with pytest.raises(ValueError, match="min_batch_size"):
        transfer_api_module.build_transfer_options(
            from_db="gp",
            to_db="trino",
            from_sql="select id from source_table",
            to_table="sandbox.target",
            batch_size=100,
            min_batch_size=101,
        )

    with pytest.raises(ValueError, match="max_batch_size"):
        transfer_api_module.build_transfer_options(
            from_db="gp",
            to_db="trino",
            from_sql="select id from source_table",
            to_table="sandbox.target",
            batch_size=100,
            max_batch_size=99,
        )

    bounded_time_options = transfer_api_module.build_transfer_options(
        from_db="gp",
        to_db="trino",
        from_sql="select id from source_table",
        to_table="sandbox.target",
        batch_size=100,
        target_batch_seconds=10,
        min_batch_seconds=15.0,
        max_batch_seconds=30.0,
    )
    assert bounded_time_options.target_batch_seconds == 15.0
    assert bounded_time_options.min_batch_seconds == 15.0
    assert bounded_time_options.max_batch_seconds == 30.0

    bounded_memory_options = transfer_api_module.build_transfer_options(
        from_db="gp",
        to_db="trino",
        from_sql="select id from source_table",
        to_table="sandbox.target",
        target_batch_memory_mb=64,
        min_batch_memory_mb=32,
        max_batch_memory_mb=512,
    )
    assert bounded_memory_options.target_batch_memory_mb == 64.0
    assert bounded_memory_options.min_batch_memory_mb == 32.0
    assert bounded_memory_options.max_batch_memory_mb == 512.0


@pytest.mark.parametrize(
    "target_batch_memory_mb",
    [0, -1, True, "64", float("nan"), float("inf")],
)
def test_transfer_options_validate_target_batch_memory(
    target_batch_memory_mb: Any,
) -> None:
    with pytest.raises(ValueError, match="target_batch_memory_mb"):
        transfer_api_module.build_transfer_options(
            from_db="gp",
            to_db="trino",
            from_sql="select id from source_table",
            to_table="sandbox.target",
            target_batch_memory_mb=target_batch_memory_mb,
        )


@pytest.mark.parametrize(
    "min_batch_seconds,max_batch_seconds",
    [
        (0, None),
        (None, 0),
        (True, None),
        ("10", None),
    ],
)
def test_transfer_options_validate_batch_seconds_bounds(
    min_batch_seconds: Any,
    max_batch_seconds: Any,
) -> None:
    match = "min_batch_seconds" if min_batch_seconds is not None else "max_batch_seconds"
    with pytest.raises(ValueError, match=match):
        transfer_api_module.build_transfer_options(
            from_db="gp",
            to_db="trino",
            from_sql="select id from source_table",
            to_table="sandbox.target",
            min_batch_seconds=min_batch_seconds,
            max_batch_seconds=max_batch_seconds,
        )


@pytest.mark.parametrize(
    "min_batch_memory_mb,max_batch_memory_mb",
    [
        (0, None),
        (None, 0),
        (True, None),
        ("16", None),
    ],
)
def test_transfer_options_validate_batch_memory_bounds(
    min_batch_memory_mb: Any,
    max_batch_memory_mb: Any,
) -> None:
    match = (
        "min_batch_memory_mb" if min_batch_memory_mb is not None else "max_batch_memory_mb"
    )
    with pytest.raises(ValueError, match=match):
        transfer_api_module.build_transfer_options(
            from_db="gp",
            to_db="trino",
            from_sql="select id from source_table",
            to_table="sandbox.target",
            min_batch_memory_mb=min_batch_memory_mb,
            max_batch_memory_mb=max_batch_memory_mb,
        )


@pytest.mark.parametrize(
    "target_rows_per_second_window",
    [0, -1, 1.2, True, "5", None],
)
def test_transfer_options_validate_rows_per_second_window(
    target_rows_per_second_window: Any,
) -> None:
    with pytest.raises(ValueError, match="target_rows_per_second_window"):
        transfer_api_module.build_transfer_options(
            from_db="gp",
            to_db="trino",
            from_sql="select id from source_table",
            to_table="sandbox.target",
            target_rows_per_second_window=target_rows_per_second_window,
        )


@pytest.mark.parametrize(
    "target_rows_per_second_deadband",
    [-0.1, float("nan"), float("inf"), True, "0.1"],
)
def test_transfer_options_validate_rows_per_second_deadband(
    target_rows_per_second_deadband: Any,
) -> None:
    with pytest.raises(ValueError, match="target_rows_per_second_deadband"):
        transfer_api_module.build_transfer_options(
            from_db="gp",
            to_db="trino",
            from_sql="select id from source_table",
            to_table="sandbox.target",
            target_rows_per_second_deadband=target_rows_per_second_deadband,
        )


def test_transfer_options_rejects_inverted_batch_seconds_bounds() -> None:
    with pytest.raises(ValueError, match="min_batch_seconds"):
        transfer_api_module.build_transfer_options(
            from_db="gp",
            to_db="trino",
            from_sql="select id from source_table",
            to_table="sandbox.target",
            min_batch_seconds=20.0,
            max_batch_seconds=10.0,
        )


def test_transfer_options_rejects_inverted_batch_memory_bounds() -> None:
    with pytest.raises(ValueError, match="min_batch_memory_mb"):
        transfer_api_module.build_transfer_options(
            from_db="gp",
            to_db="trino",
            from_sql="select id from source_table",
            to_table="sandbox.target",
            min_batch_memory_mb=64,
            max_batch_memory_mb=32,
            target_batch_memory_mb=64,
        )


def test_transfer_options_defaults_use_time_target_mode_when_not_explicit() -> None:
    options = transfer_api_module.build_transfer_options(
        from_db="gp",
        to_db="trino",
        from_sql="select id from source_table",
        to_table="sandbox.target",
        target_batch_seconds=None,
    )

    assert options.target_rows_per_second is True
    assert options.target_batch_seconds == 10.0


@pytest.mark.parametrize(
    "kwargs",
    [
        {
            "target_rows_per_second": False,
            "target_batch_seconds": 10.0,
        },
        {
            "target_rows_per_second": False,
            "target_batch_memory_mb": 16,
        },
        {
            "target_batch_seconds": 10.0,
            "target_batch_memory_mb": 16,
        },
    ],
)
def test_transfer_options_rejects_multiple_adaptation_targets(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="Only one transfer batch target"):
        transfer_api_module.build_transfer_options(
            from_db="gp",
            to_db="trino",
            from_sql="select id from source_table",
            to_table="sandbox.target",
            **kwargs,
        )


def test_load_stage_batches_fetches_row_batches_with_adaptive_sizes(monkeypatch) -> None:
    source = RecordingSourceConnection(rows=[(row_id,) for row_id in range(10)])
    connection_refs = models_module.TransferConnectionRefs(
        source={"connection": source},
        target={"connection": object()},
    )
    stage_state = models_module.TransferStageState(
        target_exists=False,
        stage_column_types={"id": "INTEGER"},
    )
    options = models_module.TransferOptions(
        from_db_key="gp",
        from_db_backend="gp",
        to_db_key="gp_sandbox",
        to_db_backend="gp",
        source_sql="select id from source_table",
        target_table="sandbox.target",
        batch_size=2,
        adaptive_batch_size=True,
        min_batch_size=1,
        max_batch_size=4,
        target_rows_per_second=False,
        target_batch_seconds=10.0,
    )
    inserted_batch_sizes: list[int] = []
    insert_durations = iter([1.0, 1.0, 30.0, 30.0])

    def fake_initialize_stage_for_first_batch(
        options: object,
        connection_refs: object,
        stage_state: object,
        batch: object,
    ) -> None:
        del options, connection_refs
        stage_state.first_non_empty_batch = batch.to_dataframe()
        stage_state.stage_table = "sandbox.target__stage__abcd1234"

    def fake_insert_rows_batch(
        connection_type: str,
        connection_ref: dict[str, Any],
        table_name: str,
        columns: list[str],
        rows: list[tuple[int]],
        **kwargs: Any,
    ) -> int:
        del connection_type, connection_ref, table_name
        assert columns == ["id"]
        assert not isinstance(rows, pd.DataFrame)
        inserted_batch_sizes.append(len(rows))
        kwargs["on_success"](next(insert_durations), len(rows))
        return len(rows)

    monkeypatch.setattr(
        attempt_module,
        "initialize_stage_for_first_batch",
        fake_initialize_stage_for_first_batch,
    )
    monkeypatch.setattr(attempt_module, "insert_rows_batch", fake_insert_rows_batch)

    total_rows = attempt_module.load_stage_batches(
        options=options,
        connection_refs=connection_refs,
        stage_state=stage_state,
        read_retry_cnt=1,
        insert_retry_cnt=1,
    )

    assert total_rows == 10
    assert inserted_batch_sizes == [2, 3, 4, 1]
    assert source.cursor_obj.fetch_sizes == [2, 3, 4, 2, 1]


def test_load_stage_batches_can_adapt_to_memory_target(monkeypatch) -> None:
    source = RecordingSourceConnection(rows=[(row_id,) for row_id in range(10)])
    connection_refs = models_module.TransferConnectionRefs(
        source={"connection": source},
        target={"connection": object()},
    )
    stage_state = models_module.TransferStageState(
        target_exists=False,
        stage_column_types={"id": "INTEGER"},
    )
    options = models_module.TransferOptions(
        from_db_key="gp",
        from_db_backend="gp",
        to_db_key="gp_sandbox",
        to_db_backend="gp",
        source_sql="select id from source_table",
        target_table="sandbox.target",
        batch_size=2,
        adaptive_batch_size=True,
        min_batch_size=1,
        max_batch_size=4,
        target_rows_per_second=False,
        target_batch_seconds=10.0,
        target_batch_memory_mb=1,
        target_batch_memory_bytes=100,
    )
    inserted_batch_sizes: list[int] = []
    memory_measurements = iter([40, 40, 300, 300])

    def fake_initialize_stage_for_first_batch(
        options: object,
        connection_refs: object,
        stage_state: object,
        batch: object,
    ) -> None:
        del options, connection_refs
        stage_state.first_non_empty_batch = batch.to_dataframe()
        stage_state.stage_table = "sandbox.target__stage__abcd1234"

    def fake_approx_memory_bytes(self: object) -> int:
        del self
        return next(memory_measurements)

    def fake_insert_rows_batch(
        connection_type: str,
        connection_ref: dict[str, Any],
        table_name: str,
        columns: list[str],
        rows: list[tuple[int]],
        **kwargs: Any,
    ) -> int:
        del connection_type, connection_ref, table_name
        assert columns == ["id"]
        inserted_batch_sizes.append(len(rows))
        kwargs["on_success"](30.0, len(rows))
        return len(rows)

    monkeypatch.setattr(
        attempt_module,
        "initialize_stage_for_first_batch",
        fake_initialize_stage_for_first_batch,
    )
    monkeypatch.setattr(
        models_module.RowBatch,
        "approx_memory_bytes",
        fake_approx_memory_bytes,
    )
    monkeypatch.setattr(attempt_module, "insert_rows_batch", fake_insert_rows_batch)

    total_rows = attempt_module.load_stage_batches(
        options=options,
        connection_refs=connection_refs,
        stage_state=stage_state,
        read_retry_cnt=1,
        insert_retry_cnt=1,
    )

    assert total_rows == 10
    assert inserted_batch_sizes == [2, 3, 4, 1]
    assert source.cursor_obj.fetch_sizes == [2, 3, 4, 1, 1]


def test_load_stage_batches_updates_progress_bar(monkeypatch) -> None:
    source = RecordingSourceConnection(rows=[(row_id,) for row_id in range(3)])
    connection_refs = models_module.TransferConnectionRefs(
        source={"connection": source},
        target={"connection": object()},
    )
    stage_state = models_module.TransferStageState(
        target_exists=False,
        stage_column_types={"id": "INTEGER"},
    )
    options = models_module.TransferOptions(
        from_db_key="gp",
        from_db_backend="gp",
        to_db_key="gp_sandbox",
        to_db_backend="gp",
        source_sql="select id from source_table",
        target_table="sandbox.target",
        batch_size=2,
        adaptive_batch_size=False,
        min_batch_size=1,
        max_batch_size=4,
        target_batch_seconds=10.0,
        progress=True,
    )
    progress_bars: list[Any] = []

    class FakeTqdm:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs
            self.updates: list[int] = []
            self.closed = False
            progress_bars.append(self)

        def update(self, value: int) -> None:
            self.updates.append(value)

        def close(self) -> None:
            self.closed = True

    def fake_initialize_stage_for_first_batch(
        options: object,
        connection_refs: object,
        stage_state: object,
        batch: object,
    ) -> None:
        del options, connection_refs
        stage_state.first_non_empty_batch = batch.to_dataframe()
        stage_state.stage_table = "sandbox.target__stage__abcd1234"

    def fake_insert_rows_batch(
        connection_type: str,
        connection_ref: dict[str, Any],
        table_name: str,
        columns: list[str],
        rows: list[tuple[int]],
        **kwargs: Any,
    ) -> int:
        del connection_type, connection_ref, table_name, columns
        kwargs["on_progress"](len(rows))
        kwargs["on_success"](1.0, len(rows))
        return len(rows)

    monkeypatch.setattr(attempt_module, "tqdm", FakeTqdm)
    monkeypatch.setattr(
        attempt_module,
        "initialize_stage_for_first_batch",
        fake_initialize_stage_for_first_batch,
    )
    monkeypatch.setattr(attempt_module, "insert_rows_batch", fake_insert_rows_batch)

    total_rows = attempt_module.load_stage_batches(
        options=options,
        connection_refs=connection_refs,
        stage_state=stage_state,
        read_retry_cnt=1,
        insert_retry_cnt=1,
    )

    assert total_rows == 3
    assert len(progress_bars) == 1
    assert progress_bars[0].kwargs == {
        "total": None,
        "desc": "transfer_table gp_sandbox.sandbox.target",
        "unit": "row",
        "disable": False,
        "bar_format": attempt_module._TRANSFER_PROGRESS_UNKNOWN_TOTAL_FORMAT,
    }
    assert progress_bars[0].updates == [2, 1]
    assert progress_bars[0].closed is True


def test_load_stage_batches_formats_transferred_row_count(
    monkeypatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = RecordingSourceConnection(rows=[(1,)])
    connection_refs = models_module.TransferConnectionRefs(
        source={"connection": source},
        target={"connection": object()},
    )
    stage_state = models_module.TransferStageState(
        target_exists=False,
        stage_column_types={"id": "INTEGER"},
    )
    options = make_progress_options(progress=False)

    def fake_initialize_stage_for_first_batch(
        options: object,
        connection_refs: object,
        stage_state: object,
        batch: object,
    ) -> None:
        del options, connection_refs
        stage_state.first_non_empty_batch = batch.to_dataframe()
        stage_state.stage_table = "sandbox.target__stage__abcd1234"

    def fake_insert_rows_batch(
        connection_type: str,
        connection_ref: dict[str, Any],
        table_name: str,
        columns: list[str],
        rows: list[tuple[int]],
        **kwargs: Any,
    ) -> int:
        batch_size = len(rows)
        del connection_type, connection_ref, table_name, columns, rows
        kwargs["on_success"](1.0, batch_size)
        return 1_000_000

    monkeypatch.setattr(
        attempt_module,
        "initialize_stage_for_first_batch",
        fake_initialize_stage_for_first_batch,
    )
    monkeypatch.setattr(attempt_module, "insert_rows_batch", fake_insert_rows_batch)

    total_rows = attempt_module.load_stage_batches(
        options=options,
        connection_refs=connection_refs,
        stage_state=stage_state,
        read_retry_cnt=1,
        insert_retry_cnt=1,
    )

    output = capsys.readouterr().out
    assert total_rows == 1_000_000
    assert (
        "[gp_sandbox/gp] "
        "Transferred batch of 1_000_000 row(s) "
        "to sandbox.target__stage__abcd1234 in 1 second "
        "(1,000,000.00 row/s); total transferred 1_000_000 row(s)"
    ) in output


def test_load_stage_batches_estimated_total_sets_progress_bar_total(
    monkeypatch,
) -> None:
    source = RecordingSourceConnection(rows=[(row_id,) for row_id in range(3)])
    connection_refs = models_module.TransferConnectionRefs(
        source={"connection": source},
        target={"connection": object()},
    )
    stage_state = models_module.TransferStageState(
        target_exists=False,
        stage_column_types={"id": "INTEGER"},
    )
    options = models_module.TransferOptions(
        from_db_key="gp",
        from_db_backend="gp",
        to_db_key="gp_sandbox",
        to_db_backend="gp",
        source_sql="select id from source_table",
        target_table="sandbox.target",
        batch_size=2,
        adaptive_batch_size=False,
        min_batch_size=1,
        max_batch_size=4,
        target_batch_seconds=10.0,
        estimate_total_rows=True,
        progress=True,
    )
    progress_bars: list[Any] = []

    class FakeTqdm:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs
            self.updates: list[int] = []
            self.closed = False
            progress_bars.append(self)

        def update(self, value: int) -> None:
            self.updates.append(value)

        def close(self) -> None:
            self.closed = True

    def fake_initialize_stage_for_first_batch(
        options: object,
        connection_refs: object,
        stage_state: object,
        batch: object,
    ) -> None:
        del options, connection_refs
        stage_state.first_non_empty_batch = batch.to_dataframe()
        stage_state.stage_table = "sandbox.target__stage__abcd1234"

    monkeypatch.setattr(attempt_module, "tqdm", FakeTqdm)
    monkeypatch.setattr(attempt_module, "estimate_source_rows", lambda *_args: 3)
    monkeypatch.setattr(
        attempt_module,
        "initialize_stage_for_first_batch",
        fake_initialize_stage_for_first_batch,
    )
    monkeypatch.setattr(
        attempt_module,
        "insert_rows_batch",
        lambda *args, **kwargs: len(args[4]),
    )

    total_rows = attempt_module.load_stage_batches(
        options=options,
        connection_refs=connection_refs,
        stage_state=stage_state,
        read_retry_cnt=1,
        insert_retry_cnt=1,
    )

    assert total_rows == 3
    assert progress_bars[0].kwargs["total"] == 3
    assert (
        progress_bars[0].kwargs["bar_format"]
        == attempt_module._TRANSFER_PROGRESS_TOTAL_FORMAT
    )
    assert progress_bars[0].updates == [2, 1]
    assert progress_bars[0].closed is True


def test_load_stage_batches_estimator_failure_keeps_unknown_total(
    monkeypatch,
) -> None:
    source = RecordingSourceConnection(rows=[(1,), (2,)])
    connection_refs = models_module.TransferConnectionRefs(
        source={"connection": source},
        target={"connection": object()},
    )
    stage_state = models_module.TransferStageState(
        target_exists=False,
        stage_column_types={"id": "INTEGER"},
    )
    options = models_module.TransferOptions(
        from_db_key="gp",
        from_db_backend="gp",
        to_db_key="gp_sandbox",
        to_db_backend="gp",
        source_sql="select id from source_table",
        target_table="sandbox.target",
        batch_size=2,
        adaptive_batch_size=False,
        min_batch_size=1,
        max_batch_size=4,
        target_batch_seconds=10.0,
        estimate_total_rows=True,
        progress=True,
    )
    progress_bars: list[Any] = []

    class FakeTqdm:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs
            self.updates: list[int] = []
            self.closed = False
            progress_bars.append(self)

        def update(self, value: int) -> None:
            self.updates.append(value)

        def close(self) -> None:
            self.closed = True

    def fake_initialize_stage_for_first_batch(
        options: object,
        connection_refs: object,
        stage_state: object,
        batch: object,
    ) -> None:
        del options, connection_refs
        stage_state.first_non_empty_batch = batch.to_dataframe()
        stage_state.stage_table = "sandbox.target__stage__abcd1234"

    monkeypatch.setattr(attempt_module, "tqdm", FakeTqdm)
    monkeypatch.setattr(
        attempt_module,
        "initialize_stage_for_first_batch",
        fake_initialize_stage_for_first_batch,
    )
    monkeypatch.setattr(
        attempt_module,
        "insert_rows_batch",
        lambda *args, **kwargs: len(args[4]),
    )

    total_rows = attempt_module.load_stage_batches(
        options=options,
        connection_refs=connection_refs,
        stage_state=stage_state,
        read_retry_cnt=1,
        insert_retry_cnt=1,
    )

    assert total_rows == 2
    assert progress_bars[0].kwargs["total"] is None
    assert progress_bars[0].updates == [2]
    assert source.cursor_obj.executed[0].startswith("EXPLAIN (FORMAT JSON)")
    assert source.cursor_obj.executed[-1] == "select id from source_table"


def test_load_stage_batches_progress_false_disables_bar(monkeypatch) -> None:
    source = RecordingSourceConnection(rows=[(1,), (2,)])
    connection_refs = models_module.TransferConnectionRefs(
        source={"connection": source},
        target={"connection": object()},
    )
    stage_state = models_module.TransferStageState(
        target_exists=False,
        stage_column_types={"id": "INTEGER"},
    )
    options = models_module.TransferOptions(
        from_db_key="gp",
        from_db_backend="gp",
        to_db_key="gp_sandbox",
        to_db_backend="gp",
        source_sql="select id from source_table",
        target_table="sandbox.target",
        batch_size=2,
        adaptive_batch_size=False,
        min_batch_size=1,
        max_batch_size=4,
        target_batch_seconds=10.0,
        progress=False,
        estimate_total_rows=True,
    )
    progress_bars: list[Any] = []

    class FakeTqdm:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs
            self.updates: list[int] = []
            self.closed = False
            progress_bars.append(self)

        def update(self, value: int) -> None:
            self.updates.append(value)

        def close(self) -> None:
            self.closed = True

    def fake_initialize_stage_for_first_batch(
        options: object,
        connection_refs: object,
        stage_state: object,
        batch: object,
    ) -> None:
        del options, connection_refs
        stage_state.first_non_empty_batch = batch.to_dataframe()
        stage_state.stage_table = "sandbox.target__stage__abcd1234"

    def unexpected_estimate(*_args: object) -> int:
        raise AssertionError("unexpected estimate")

    monkeypatch.setattr(attempt_module, "tqdm", FakeTqdm)
    monkeypatch.setattr(attempt_module, "estimate_source_rows", unexpected_estimate)
    monkeypatch.setattr(
        attempt_module,
        "initialize_stage_for_first_batch",
        fake_initialize_stage_for_first_batch,
    )
    monkeypatch.setattr(
        attempt_module,
        "insert_rows_batch",
        lambda *args, **kwargs: len(args[4]),
    )

    total_rows = attempt_module.load_stage_batches(
        options=options,
        connection_refs=connection_refs,
        stage_state=stage_state,
        read_retry_cnt=1,
        insert_retry_cnt=1,
    )

    assert total_rows == 2
    assert len(progress_bars) == 1
    assert progress_bars[0].kwargs["disable"] is True
    assert progress_bars[0].updates == [2]
    assert progress_bars[0].closed is True


def test_transfer_progress_bar_formats_unknown_total_counts(monkeypatch) -> None:
    progress_bars = capture_rendering_progress_bars(monkeypatch)

    options = make_progress_options()
    progress_bar = attempt_module._make_transfer_progress_bar(options, total=None)
    progress_bar.update(1_722_355)

    assert progress_bars[0].rendered == [
        "transfer_table gp_sandbox.sandbox.target: "
        "1_722_355row [00:00, 14087.46row/s]"
    ]


def test_transfer_progress_bar_formats_estimated_total_counts(monkeypatch) -> None:
    progress_bars = capture_rendering_progress_bars(monkeypatch)

    options = make_progress_options()
    progress_bar = attempt_module._make_transfer_progress_bar(
        options,
        total=2_000_000,
    )
    progress_bar.update(1_722_355)

    assert progress_bars[0].rendered == [
        "transfer_table gp_sandbox.sandbox.target:  86%|########| "
        "1_722_355/2_000_000 [00:00<00:02, 14087.46row/s]"
    ]


def test_transfer_progress_bar_progress_false_disables_output(monkeypatch) -> None:
    progress_bars = capture_rendering_progress_bars(monkeypatch)

    options = make_progress_options(progress=False)
    progress_bar = attempt_module._make_transfer_progress_bar(options, total=None)
    progress_bar.update(1_722_355)

    assert progress_bars[0].kwargs["disable"] is True
    assert progress_bars[0].rendered == []


@pytest.mark.parametrize("progress", [None, 0, 1, "yes"])
def test_transfer_table_validates_progress(progress: Any) -> None:
    with pytest.raises(ValueError, match="progress"):
        transfer_api_module.transfer_table(
            from_db="gp",
            to_db="trino",
            from_sql="select id from source_table",
            to_table="sandbox.target",
            dry_run=True,
            progress=progress,
        )


@pytest.mark.parametrize("estimate_total_rows", [None, 0, 1, "yes"])
def test_transfer_table_validates_estimate_total_rows(
    estimate_total_rows: Any,
) -> None:
    with pytest.raises(ValueError, match="estimate_total_rows"):
        transfer_api_module.transfer_table(
            from_db="gp",
            to_db="trino",
            from_sql="select id from source_table",
            to_table="sandbox.target",
            dry_run=True,
            estimate_total_rows=estimate_total_rows,
        )


def test_transfer_dry_run_includes_estimate_total_rows_option() -> None:
    plan = transfer_api_module.transfer_table(
        from_db="gp",
        to_db="trino",
        from_sql="select id from source_table",
        to_table="sandbox.target",
        dry_run=True,
        estimate_total_rows=True,
        target_batch_memory_mb=32,
    )

    assert plan.options["estimate_total_rows"] is True
    assert plan.options["target_rows_per_second_window"] == 5
    assert plan.options["target_rows_per_second_deadband"] == 0.15
    assert plan.options["target_batch_memory_mb"] == 32.0
    assert plan.options["max_batch_size"] is None

    plan = transfer_api_module.transfer_table(
        from_db="gp",
        to_db="trino",
        from_sql="select id from source_table",
        to_table="sandbox.target",
        dry_run=True,
        target_rows_per_second_window=3,
        target_rows_per_second_deadband=0.05,
    )
    assert plan.options["target_rows_per_second_window"] == 3
    assert plan.options["target_rows_per_second_deadband"] == 0.05


@pytest.mark.parametrize(
    ("backend", "connection", "expected_total", "expected_sql_prefix"),
    [
        (
            "gp",
            StaticDbapiConnection([('[{"Plan": {"Plan Rows": 123}}]',)]),
            123,
            "EXPLAIN (FORMAT JSON)",
        ),
        (
            "trino",
            StaticDbapiConnection([('{"outputRowCount": 456}',)]),
            456,
            "EXPLAIN (TYPE DISTRIBUTED, FORMAT JSON)",
        ),
        (
            "ch",
            StaticClickHouseClient([("default", "source_table", 1, 789, 1)]),
            789,
            "EXPLAIN ESTIMATE",
        ),
    ],
)
def test_estimate_source_rows_uses_backend_planner_estimates(
    backend: str,
    connection: Any,
    expected_total: int,
    expected_sql_prefix: str,
) -> None:
    options = models_module.TransferOptions(
        from_db_key=backend,
        from_db_backend=backend,
        to_db_key="gp_sandbox",
        to_db_backend="gp",
        source_sql="select id from source_table",
        target_table="sandbox.target",
        estimate_total_rows=True,
        progress=True,
    )

    estimated_total = estimate_module.estimate_source_rows(options, connection)

    assert estimated_total == expected_total
    executed = getattr(connection, "executed", getattr(connection, "queries", []))
    assert executed[0].startswith(expected_sql_prefix)


def test_clickhouse_estimator_skips_non_simple_select() -> None:
    connection = StaticClickHouseClient([("default", "source_table", 1, 789, 1)])
    options = models_module.TransferOptions(
        from_db_key="ch",
        from_db_backend="ch",
        to_db_key="gp_sandbox",
        to_db_backend="gp",
        source_sql="select id from source_table where id > 10",
        target_table="sandbox.target",
        estimate_total_rows=True,
    )

    assert estimate_module.estimate_source_rows(options, connection) is None
    assert connection.queries == []
