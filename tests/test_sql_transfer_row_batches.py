from __future__ import annotations

import importlib
import io
import threading
import warnings
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from types import SimpleNamespace

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

attempt_module = importlib.import_module(
    "analytics_toolkit.sql.dml.transfer.flow.attempt"
)
config_module = importlib.import_module("analytics_toolkit.sql.connection.config")
finalize_module = importlib.import_module(
    "analytics_toolkit.sql.dml.transfer.flow.finalize"
)
parquet_stage_module = importlib.import_module(
    "analytics_toolkit.sql.dml.transfer.flow.parquet_stage"
)
keys_module = importlib.import_module("analytics_toolkit.sql.dml.transfer.flow.keys")
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


def make_gp_config(connection_key: str) -> Any:
    return config_module.GpConfig(
        connection_key=connection_key,
        backend="gp",
        host="gp.example",
        port=5432,
        user="source_user",
        password="password",
        database="db",
        connect_timeout=30,
        keepalives=True,
        keepalives_idle=60,
        keepalives_interval=10,
        keepalives_count=3,
        sslmode=None,
        ca_certs=[],
        ssl_cert=None,
        ssl_key=None,
        transfer_staging_schema=None,
    )


def make_ch_config(connection_key: str) -> Any:
    return config_module.ChConfig(
        connection_key=connection_key,
        backend="ch",
        host="ch.example",
        port=8123,
        user="source_user",
        password="password",
        database="default",
        secure=False,
        verify_value=None,
        ca_certs=[],
        ca_certs_variable=None,
        connect_timeout=None,
        send_receive_timeout=None,
        settings=None,
        interface=None,
        query_limit=None,
        query_retries=None,
        client_name=None,
        transfer_staging_schema=None,
    )


def make_trino_config(
    connection_key: str,
    *,
    transfer_staging_schema: str | None = "object_storage.sandbox",
    transfer_staging_location: str | None = "s3://bucket/tmp/analytics_toolkit_transfer",
) -> Any:
    return config_module.TrinoConfig(
        connection_key=connection_key,
        backend="trino",
        host="trino.example",
        port=8080,
        user="target_user",
        password="password",
        catalog="iceberg",
        schema="sandbox",
        auth_mode="basic",
        http_scheme="https",
        verify_value="true",
        ca_certs=[],
        insert_chunk_size=None,
        request_timeout=None,
        source=None,
        transfer_staging_schema=transfer_staging_schema,
        transfer_staging_location=transfer_staging_location,
    )


def test_normalize_transfer_slices_accepts_single_key_sequence_values() -> None:
    keys, values, slices, concurrency = keys_module.normalize_transfer_slices(
        source_sql="select id, event_date from events;",
        transfer_keys="event_date",
        transfer_key_values=["2025-01-01", "2025-01-02"],
        concurrency=2,
    )

    assert keys == ["event_date"]
    assert values == {"event_date": ["2025-01-01", "2025-01-02"]}
    assert concurrency == 2
    assert [transfer_slice.values for transfer_slice in slices] == [
        ("2025-01-01",),
        ("2025-01-02",),
    ]
    assert "FROM (select id, event_date from events)" in slices[0].source_sql
    assert "(event_date) = '2025-01-01'" in slices[0].source_sql


def test_normalize_transfer_slices_accepts_single_key_mapping_values() -> None:
    keys, values, slices, _concurrency = keys_module.normalize_transfer_slices(
        source_sql="select id from events",
        transfer_keys="event_date",
        transfer_key_values={"event_date": ["2025-01-01"]},
        concurrency=1,
    )

    assert keys == ["event_date"]
    assert values == {"event_date": ["2025-01-01"]}
    assert [transfer_slice.values for transfer_slice in slices] == [("2025-01-01",)]


def test_normalize_transfer_slices_builds_multi_key_cartesian_values() -> None:
    keys, values, slices, _concurrency = keys_module.normalize_transfer_slices(
        source_sql="select id, event_date from events",
        transfer_keys=["event_date", "right(user_id, 1)"],
        transfer_key_values={
            "event_date": ["2025-01-01", "2025-01-02"],
            "right(user_id, 1)": ["0", "1"],
        },
        concurrency=3,
    )

    assert keys == ["event_date", "right(user_id, 1)"]
    assert values == {
        "event_date": ["2025-01-01", "2025-01-02"],
        "right(user_id, 1)": ["0", "1"],
    }
    assert [transfer_slice.values for transfer_slice in slices] == [
        ("2025-01-01", "0"),
        ("2025-01-01", "1"),
        ("2025-01-02", "0"),
        ("2025-01-02", "1"),
    ]
    assert "(event_date) = '2025-01-01'\n  AND (right(user_id, 1)) = '0'" in (
        slices[0].predicate_sql
    )


@pytest.mark.parametrize(
    ("transfer_keys", "transfer_key_values", "concurrency", "match"),
    [
        ("event_date", ["2025-01-01", "2025-01-01"], 1, "duplicate"),
        ("event_date", [], 1, "must not be empty"),
        (["event_date", "bucket"], {"event_date": ["2025-01-01"]}, 1, "missing"),
        ("event_date", {"event_date": ["2025-01-01"], "bucket": ["0"]}, 1, "extra"),
        (None, ["2025-01-01"], 1, "requires transfer_keys"),
        ("event_date", None, 1, "requires explicit"),
        (None, None, 2, "concurrency > 1"),
        ("event_date", ["2025-01-01"], 0, "positive integer"),
        ("event_date", ["2025-01-01"], True, "positive integer"),
    ],
)
def test_normalize_transfer_slices_rejects_invalid_inputs(
    transfer_keys: Any,
    transfer_key_values: Any,
    concurrency: Any,
    match: str,
) -> None:
    with pytest.raises(ValueError, match=match):
        keys_module.normalize_transfer_slices(
            source_sql="select id from events",
            transfer_keys=transfer_keys,
            transfer_key_values=transfer_key_values,
            concurrency=concurrency,
        )


def test_transfer_slice_query_literals_and_wrapping() -> None:
    values = (
        "Bob's",
        date(2025, 1, 1),
        datetime(2025, 1, 1, 12, 30, 1),
        7,
        1.5,
        True,
        Decimal("10.25"),
        None,
    )
    transfer_slice = keys_module.build_transfer_slice(
        index=3,
        source_sql="select * from events",
        transfer_keys=["name", "dt", "ts", "id", "score", "active", "amount", "deleted_at"],
        values=values,
    )

    assert transfer_slice.source_sql.startswith("SELECT *\nFROM (select * from events)")
    assert "(name) = 'Bob''s'" in transfer_slice.predicate_sql
    assert "(dt) = DATE '2025-01-01'" in transfer_slice.predicate_sql
    assert "(ts) = TIMESTAMP '2025-01-01 12:30:01'" in transfer_slice.predicate_sql
    assert "(id) = 7" in transfer_slice.predicate_sql
    assert "(score) = 1.5" in transfer_slice.predicate_sql
    assert "(active) = TRUE" in transfer_slice.predicate_sql
    assert "(amount) = 10.25" in transfer_slice.predicate_sql
    assert "(deleted_at) IS NULL" in transfer_slice.predicate_sql
    assert "\n  AND " in transfer_slice.predicate_sql


def test_normalize_transfer_slices_rejects_multi_statement_source_sql() -> None:
    with pytest.raises(ValueError, match="exactly one SQL statement"):
        keys_module.normalize_transfer_slices(
            source_sql="select 1; select 2",
            transfer_keys="id",
            transfer_key_values=[1],
            concurrency=1,
        )


def test_run_transfer_attempt_cleans_only_current_stage_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
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

    def fake_cleanup_stale_stage_tables_with_connection(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("transfer must not run stale stage discovery cleanup")

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
        raising=False,
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
    assert events == [
        "create_stage_state",
        "inspect_source_query_schema",
        "load_stage_batches",
        "finalize_loaded_stage",
        "cleanup_stage",
        "close:source",
        "close:target",
    ]


def test_run_transfer_attempt_skips_stale_cleanup_when_staging_schema_is_missing(
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

    def fake_cleanup_stale_stage_tables_with_connection(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("transfer must not run stale stage discovery cleanup")

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
        raising=False,
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
        "inspect_source_query_schema",
        "load_stage_batches",
        "finalize_loaded_stage",
        "cleanup_stage",
        "close:source",
        "close:target",
    ]


def make_keyed_options(**overrides: Any) -> Any:
    _keys, values, slices, concurrency = keys_module.normalize_transfer_slices(
        source_sql="select id, event_date from source_table",
        transfer_keys="event_date",
        transfer_key_values=["2025-01-01", "2025-01-02"],
        concurrency=overrides.pop("concurrency", 1),
    )
    option_values = {
        "from_db_key": "source_db",
        "from_db_backend": "gp",
        "to_db_key": "target_db",
        "to_db_backend": "gp",
        "source_sql": "select id, event_date from source_table",
        "target_table": "sandbox.target",
        "batch_size": 2,
        "transfer_keys": ["event_date"],
        "transfer_key_values": values,
        "transfer_slices": slices,
        "concurrency": concurrency,
    }
    option_values.update(overrides)
    return models_module.TransferOptions(**option_values)


def test_run_keyed_transfer_attempt_uses_one_stage_finalize_and_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    source_conn = FakeTransferConnection("main-source")
    target_conn = FakeTransferConnection("main-target")
    options = make_keyed_options()

    def fake_get_sql_connection(connection_key: str) -> FakeTransferConnection:
        if connection_key == "source_db":
            return source_conn
        if connection_key == "target_db":
            return target_conn
        raise AssertionError(f"unexpected connection key: {connection_key}")

    def fake_create_stage_state(*_args: Any, **_kwargs: Any) -> Any:
        events.append("create_stage_state")
        return models_module.TransferStageState(target_exists=False)

    def fake_inspect_source_query_schema(*_args: Any, **_kwargs: Any) -> list[Any]:
        events.append("inspect_source_query_schema")
        return [SimpleNamespace(name="id", native_type="integer")]

    def fake_initialize_shared_stage_for_keyed_slices(**kwargs: Any) -> None:
        events.append("initialize_shared_stage")
        stage_state = kwargs["stage_state"]
        stage_state.stage_table = "sandbox.target__stage__abcd1234"
        stage_state.stage_table_created = True
        stage_state.stage_column_types = {"id": "INTEGER"}
        stage_state.first_non_empty_batch = pd.DataFrame(columns=["id"])

    def fake_load_keyed_stage_slices(**_kwargs: Any) -> int:
        events.append("load_keyed_stage_slices")
        return 5

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
        "inspect_source_query_schema",
        fake_inspect_source_query_schema,
    )
    monkeypatch.setattr(
        attempt_module,
        "initialize_shared_stage_for_keyed_slices",
        fake_initialize_shared_stage_for_keyed_slices,
    )
    monkeypatch.setattr(
        attempt_module,
        "load_keyed_stage_slices",
        fake_load_keyed_stage_slices,
    )
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

    assert total_rows == 5
    assert events == [
        "create_stage_state",
        "inspect_source_query_schema",
        "initialize_shared_stage",
        "load_keyed_stage_slices",
        "finalize_loaded_stage",
        "cleanup_stage",
        "close:source",
        "close:target",
    ]


def test_keyed_slice_workers_use_filtered_sql_and_own_connections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = make_keyed_options()
    stage_state = models_module.TransferStageState(
        target_exists=False,
        stage_table_created=True,
        first_non_empty_batch=pd.DataFrame(columns=["id", "event_date"]),
        stage_column_types={"id": "INTEGER", "event_date": "DATE"},
        stage_table="sandbox.target__stage__abcd1234",
    )
    opened_connections: list[tuple[str, str]] = []
    loaded: list[dict[str, Any]] = []

    def fake_get_sql_connection(connection_key: str) -> FakeTransferConnection:
        connection = FakeTransferConnection(f"{connection_key}-{len(opened_connections)}")
        opened_connections.append((connection_key, connection.name))
        return connection

    def fake_load_stage_batches(**kwargs: Any) -> int:
        loaded.append(
            {
                "source_sql": kwargs["options"].source_sql,
                "source_conn": kwargs["connection_refs"].source["connection"].name,
                "target_conn": kwargs["connection_refs"].target["connection"].name,
                "slice_index": kwargs["slice_index"],
            }
        )
        return kwargs["slice_index"] + 1

    monkeypatch.setattr(attempt_module, "get_sql_connection", fake_get_sql_connection)
    monkeypatch.setattr(attempt_module, "load_stage_batches", fake_load_stage_batches)

    total_rows = attempt_module.load_keyed_stage_slices(
        options=options,
        stage_state=stage_state,
        read_retry_cnt=1,
        insert_retry_cnt=1,
    )

    assert total_rows == 3
    assert [item["source_sql"] for item in loaded] == [
        transfer_slice.source_sql for transfer_slice in options.transfer_slices
    ]
    assert [item["slice_index"] for item in loaded] == [0, 1]
    assert opened_connections == [
        ("source_db", "source_db-0"),
        ("target_db", "target_db-1"),
        ("source_db", "source_db-2"),
        ("target_db", "target_db-3"),
    ]
    assert loaded[0]["source_conn"] != loaded[1]["source_conn"]
    assert loaded[0]["target_conn"] != loaded[1]["target_conn"]


def test_keyed_worker_failure_skips_finalize_and_still_cleans_stage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    options = make_keyed_options()

    monkeypatch.setattr(
        attempt_module,
        "get_sql_connection",
        lambda key: FakeTransferConnection(key),
    )
    monkeypatch.setattr(
        attempt_module,
        "create_stage_state",
        lambda *_args, **_kwargs: models_module.TransferStageState(target_exists=False),
    )
    monkeypatch.setattr(
        attempt_module,
        "inspect_source_query_schema",
        lambda *_args, **_kwargs: [SimpleNamespace(name="id", native_type="integer")],
    )

    def fake_initialize_shared_stage_for_keyed_slices(**kwargs: Any) -> None:
        stage_state = kwargs["stage_state"]
        stage_state.stage_table = "sandbox.target__stage__abcd1234"
        stage_state.stage_table_created = True
        stage_state.stage_column_types = {"id": "INTEGER"}
        stage_state.first_non_empty_batch = pd.DataFrame(columns=["id"])

    def fake_load_keyed_stage_slices(**_kwargs: Any) -> int:
        events.append("load_keyed_stage_slices")
        raise RuntimeError("slice failed")

    def fail_finalize(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("target must not be finalized after slice failure")

    def fake_cleanup_stage(*_args: Any, **_kwargs: Any) -> None:
        events.append("cleanup_stage")

    monkeypatch.setattr(
        attempt_module,
        "initialize_shared_stage_for_keyed_slices",
        fake_initialize_shared_stage_for_keyed_slices,
    )
    monkeypatch.setattr(
        attempt_module,
        "load_keyed_stage_slices",
        fake_load_keyed_stage_slices,
    )
    monkeypatch.setattr(attempt_module, "finalize_loaded_stage", fail_finalize)
    monkeypatch.setattr(attempt_module, "cleanup_stage", fake_cleanup_stage)

    with pytest.raises(RuntimeError, match="slice failed"):
        attempt_module.run_transfer_attempt(
            options=options,
            read_retry_cnt=1,
            insert_retry_cnt=1,
        )

    assert events == ["load_keyed_stage_slices", "cleanup_stage"]


def test_keyed_transfer_workers_run_concurrently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = make_keyed_options(concurrency=2)
    stage_state = models_module.TransferStageState(
        target_exists=False,
        stage_table_created=True,
        first_non_empty_batch=pd.DataFrame(columns=["id", "event_date"]),
        stage_column_types={"id": "INTEGER", "event_date": "DATE"},
        stage_table="sandbox.target__stage__abcd1234",
    )
    barrier = threading.Barrier(2)
    started: list[int] = []

    def fake_load_keyed_stage_slice(**kwargs: Any) -> int:
        started.append(kwargs["transfer_slice"].index)
        barrier.wait(timeout=2)
        return 1

    monkeypatch.setattr(
        attempt_module,
        "load_keyed_stage_slice",
        fake_load_keyed_stage_slice,
    )

    total_rows = attempt_module.load_keyed_stage_slices(
        options=options,
        stage_state=stage_state,
        read_retry_cnt=1,
        insert_retry_cnt=1,
    )

    assert total_rows == 2
    assert sorted(started) == [0, 1]


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


def test_cleanup_stale_stage_tables_empty_explicit_list_drops_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cleanup_called = 0
    query_called = 0

    def fake_query_gp_stage_tables(
        transfer_staging_schema: str,
        table_prefix: str,
        connection: Any,
    ) -> list[str]:
        nonlocal query_called
        query_called += 1
        del transfer_staging_schema, table_prefix, connection
        return ["target__analytics_toolkit_target_user__stage__stale"]

    def fake_cleanup_stage_table_with_retry(*_args: Any, **_kwargs: Any) -> None:
        nonlocal cleanup_called
        cleanup_called += 1

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
        fake_cleanup_stage_table_with_retry,
    )

    staging_module.cleanup_stale_stage_tables_with_connection(
        db_key="gp",
        target_table="analytics.target",
        connection_ref={"connection": object()},
        read_retry_cnt=3,
        stage_tables=[],
    )

    assert cleanup_called == 0
    assert query_called == 0


def test_cleanup_stale_stage_tables_rejects_unqualified_explicit_without_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        staging_module,
        "get_connection_config",
        lambda db_key: SimpleNamespace(
            connection_key=db_key,
            backend="gp",
            transfer_staging_schema=None,
            user="target_user",
        ),
    )

    with pytest.raises(
        staging_module.InvalidSqlInputError,
        match="Unqualified stage table names require transfer_staging_schema",
    ):
        staging_module.cleanup_stale_stage_tables_with_connection(
            db_key="gp",
            target_table="analytics.target",
            connection_ref={"connection": object()},
            read_retry_cnt=3,
            stage_tables=["target__analytics_toolkit_target_user__stage__implicit"],
        )


def test_cleanup_stale_stage_tables_preserves_trino_catalog_schema_for_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    discovered: list[str] = []
    monkeypatch.setattr(
        staging_module,
        "get_connection_config",
        lambda db_key: SimpleNamespace(
            connection_key=db_key,
            backend="trino",
            transfer_staging_schema="hive.scratch",
            user="target_user",
        ),
    )
    monkeypatch.setattr(
        staging_module,
        "cleanup_stage_table_with_retry",
        lambda *args, **kwargs: discovered.append(args[3]),
    )

    staging_module.cleanup_stale_stage_tables_with_connection(
        db_key="trino",
        target_table="analytics.target",
        connection_ref={"connection": object()},
        read_retry_cnt=3,
        stage_tables=[
            "stage_x",
            "iceberg.scratch.stage_y",
        ],
    )

    assert discovered == [
        "hive.scratch.stage_x",
        "iceberg.scratch.stage_y",
    ]


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


@pytest.mark.parametrize(
    ("source_key", "source_config"),
    [
        ("gp", make_gp_config("gp")),
        ("ch", make_ch_config("ch")),
        ("trino_a", make_trino_config("trino_a")),
    ],
)
def test_transfer_options_enable_parquet_staging_for_trino_target_with_location(
    monkeypatch: pytest.MonkeyPatch,
    source_key: str,
    source_config: Any,
) -> None:
    target_config = make_trino_config("trino_b")
    configs = {source_key: source_config, "trino_b": target_config}
    monkeypatch.setattr(
        transfer_api_module,
        "get_connection_config",
        lambda db_key: configs[db_key],
    )

    options = transfer_api_module.build_transfer_options(
        from_db=source_key,
        to_db="trino_b",
        from_sql="select id from source_table",
        to_table="sandbox.target",
    )

    assert options.trino_mode == "parquet"
    assert options.transfer_staging_schema == "object_storage.sandbox"
    assert (
        options.transfer_staging_location
        == "s3://bucket/tmp/analytics_toolkit_transfer"
    )


def test_transfer_options_keep_row_batch_staging_when_trino_location_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configs = {
        "gp": make_gp_config("gp"),
        "trino": make_trino_config("trino", transfer_staging_location=None),
    }
    monkeypatch.setattr(
        transfer_api_module,
        "get_connection_config",
        lambda db_key: configs[db_key],
    )

    options = transfer_api_module.build_transfer_options(
        from_db="gp",
        to_db="trino",
        from_sql="select id from source_table",
        to_table="sandbox.target",
    )

    assert options.trino_mode == "values"
    assert options.transfer_staging_schema == "object_storage.sandbox"
    assert options.transfer_staging_location is None


def test_transfer_options_explicit_values_mode_disables_parquet_staging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configs = {
        "gp": make_gp_config("gp"),
        "trino": make_trino_config("trino"),
    }
    monkeypatch.setattr(
        transfer_api_module,
        "get_connection_config",
        lambda db_key: configs[db_key],
    )

    options = transfer_api_module.build_transfer_options(
        from_db="gp",
        to_db="trino",
        from_sql="select id from source_table",
        to_table="sandbox.target",
        trino_mode="values",
    )

    assert options.trino_mode == "values"
    assert options.transfer_staging_schema == "object_storage.sandbox"
    assert (
        options.transfer_staging_location
        == "s3://bucket/tmp/analytics_toolkit_transfer"
    )


def test_transfer_options_reject_explicit_parquet_without_location(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configs = {
        "gp": make_gp_config("gp"),
        "trino": make_trino_config("trino", transfer_staging_location=None),
    }
    monkeypatch.setattr(
        transfer_api_module,
        "get_connection_config",
        lambda db_key: configs[db_key],
    )

    with pytest.raises(ValueError, match="transfer_staging_location"):
        transfer_api_module.build_transfer_options(
            from_db="gp",
            to_db="trino",
            from_sql="select id from source_table",
            to_table="sandbox.target",
            trino_mode="parquet",
        )


def test_transfer_options_reject_explicit_mode_for_non_trino_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configs = {
        "source": make_gp_config("source"),
        "target": make_gp_config("target"),
    }
    monkeypatch.setattr(
        transfer_api_module,
        "get_connection_config",
        lambda db_key: configs[db_key],
    )

    with pytest.raises(ValueError, match="to_db has type 'trino'"):
        transfer_api_module.build_transfer_options(
            from_db="source",
            to_db="target",
            from_sql="select id from source_table",
            to_table="sandbox.target",
            trino_mode="values",
        )


def test_transfer_options_reject_invalid_trino_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configs = {
        "gp": make_gp_config("gp"),
        "trino": make_trino_config("trino"),
    }
    monkeypatch.setattr(
        transfer_api_module,
        "get_connection_config",
        lambda db_key: configs[db_key],
    )

    with pytest.raises(ValueError, match="trino_mode"):
        transfer_api_module.build_transfer_options(
            from_db="gp",
            to_db="trino",
            from_sql="select id from source_table",
            to_table="sandbox.target",
            trino_mode="execute_values",
        )


def test_transfer_options_reject_same_key_before_parquet_staging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = make_trino_config("trino")
    monkeypatch.setattr(
        transfer_api_module,
        "get_connection_config",
        lambda db_key: config,
    )

    with pytest.raises(ValueError, match="from_db and to_db must be different"):
        transfer_api_module.build_transfer_options(
            from_db="trino",
            to_db="trino",
            from_sql="select id from source_table",
            to_table="sandbox.target",
        )


def test_transfer_dry_run_shows_parquet_stage_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configs = {
        "gp": make_gp_config("gp"),
        "trino": make_trino_config("trino"),
    }
    monkeypatch.setattr(
        transfer_api_module,
        "get_connection_config",
        lambda db_key: configs[db_key],
    )

    plan = transfer_api_module.transfer_table(
        from_db="gp",
        to_db="trino",
        from_sql="select id from source_table",
        to_table="sandbox.target",
        table_schema={"id": "BIGINT"},
        dry_run=True,
    )

    assert plan.options["trino_mode"] == "parquet"
    assert "use_parquet_staging" not in plan.options
    assert plan.metadata.stage_table.startswith("object_storage.sandbox.target__")
    assert plan.metadata.stage_external_location == (
        "s3://bucket/tmp/analytics_toolkit_transfer/target/"
        "__analytics_toolkit_target_user__stage__dryrun/"
    )
    assert any(
        sql.startswith("CREATE TABLE object_storage.sandbox.target__")
        and "external_location = 's3://bucket/tmp/analytics_toolkit_transfer/target/"
        in sql
        for sql in plan.sqls
    )
    assert any(
        sql.startswith("WRITE PARQUET FILES TO ")
        and "__analytics_toolkit_target_user__stage__dryrun/" in sql
        for sql in plan.sqls
    )
    assert any(sql.startswith("INSERT INTO sandbox.target") for sql in plan.sqls)
    assert any(
        sql.startswith("DROP TABLE IF EXISTS object_storage.sandbox")
        for sql in plan.sqls
    )
    assert any(
        sql.startswith("DELETE STAGE FILES s3://bucket/tmp") for sql in plan.sqls
    )


def test_transfer_dry_run_values_mode_uses_row_stage_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configs = {
        "gp": make_gp_config("gp"),
        "trino": make_trino_config("trino"),
    }
    monkeypatch.setattr(
        transfer_api_module,
        "get_connection_config",
        lambda db_key: configs[db_key],
    )

    plan = transfer_api_module.transfer_table(
        from_db="gp",
        to_db="trino",
        from_sql="select id from source_table",
        to_table="sandbox.target",
        table_schema={"id": "BIGINT"},
        trino_mode="values",
        dry_run=True,
    )

    assert plan.options["trino_mode"] == "values"
    assert "use_parquet_staging" not in plan.options
    assert plan.metadata.stage_external_location is None
    assert not any(sql.startswith("WRITE PARQUET FILES TO ") for sql in plan.sqls)
    assert not any(sql.startswith("DELETE STAGE FILES ") for sql in plan.sqls)
    assert any(
        sql.startswith("INSERT INTO ")
        and " SELECT * FROM (<source batches>)" in sql
        for sql in plan.sqls
    )


def test_transfer_dry_run_shows_keyed_slice_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configs = {
        "source": make_gp_config("source"),
        "target": make_gp_config("target"),
    }
    monkeypatch.setattr(
        transfer_api_module,
        "get_connection_config",
        lambda db_key: configs[db_key],
    )

    plan = transfer_api_module.transfer_table(
        from_db="source",
        to_db="target",
        from_sql="select id, event_date from source_table;",
        to_table="sandbox.target",
        table_schema={"id": "INTEGER", "event_date": "DATE"},
        transfer_keys="event_date",
        transfer_key_values=["2025-01-01", "2025-01-02"],
        concurrency=2,
        dry_run=True,
    )

    assert plan.options["transfer_keys"] == ["event_date"]
    assert plan.options["transfer_key_values"] == {
        "event_date": ["2025-01-01", "2025-01-02"]
    }
    assert plan.options["concurrency"] == 2
    assert plan.options["transfer_slice_count"] == 2
    read_source_sqls = [
        statement.sql for statement in plan.statements if statement.phase == "read_source"
    ]
    assert len(read_source_sqls) == 2
    assert all(
        "FROM (select id, event_date from source_table) AS "
        "analytics_toolkit_transfer_source" in sql
        for sql in read_source_sqls
    )
    assert "(event_date) = '2025-01-01'" in read_source_sqls[0]
    assert "(event_date) = '2025-01-02'" in read_source_sqls[1]
    assert any("shared keyed source slice batches" in sql for sql in plan.sqls)


def test_transfer_dry_run_upsert_uses_parquet_stage_table_in_merge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configs = {
        "gp": make_gp_config("gp"),
        "trino": make_trino_config("trino"),
    }
    monkeypatch.setattr(
        transfer_api_module,
        "get_connection_config",
        lambda db_key: configs[db_key],
    )

    plan = transfer_api_module.transfer_table(
        from_db="gp",
        to_db="trino",
        from_sql="select id, amount from source_table",
        to_table="sandbox.target",
        write_mode="upsert",
        key_columns=["id"],
        table_schema={"id": "BIGINT", "amount": "DOUBLE"},
        dry_run=True,
    )

    assert any(
        sql.startswith("MERGE INTO sandbox.target AS target_dst\n")
        and "USING object_storage.sandbox.target__" in sql
        for sql in plan.sqls
    )


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


def test_adaptive_batch_sizer_first_rows_per_second_sample_schedules_shrink() -> None:
    sizer = models_module.AdaptiveBatchSizer(
        enabled=True,
        current_size=100,
        min_size=1,
        max_size=200,
        target_seconds=10.0,
        optimize_by_rows_per_second=True,
        target_rows_per_second_window=1,
        target_rows_per_second_deadband=0.1,
    )

    sizer.update(1.0, inserted_rows=100)

    assert sizer.current_size == 90
    assert sizer.baseline_size == 100
    assert sizer.baseline_rows_per_second == 100.0
    assert sizer.probe_direction == "shrink"
    assert sizer.is_experimental_size is True


def test_adaptive_batch_sizer_shrink_equivalent_restores_and_switches_to_grow() -> None:
    sizer = models_module.AdaptiveBatchSizer(
        enabled=True,
        current_size=100,
        min_size=1,
        max_size=200,
        target_seconds=10.0,
        optimize_by_rows_per_second=True,
        target_rows_per_second_window=1,
        target_rows_per_second_deadband=0.1,
    )

    sizer.update(1.0, inserted_rows=100)
    sizer.update(0.9, inserted_rows=90)

    assert sizer.current_size == 110
    assert sizer.baseline_size == 100
    assert sizer.baseline_rows_per_second == 100.0
    assert sizer.probe_direction == "grow"
    assert sizer.is_experimental_size is True


def test_adaptive_batch_sizer_grow_equivalent_accepts_and_continues_growing() -> None:
    sizer = models_module.AdaptiveBatchSizer(
        enabled=True,
        current_size=100,
        min_size=1,
        max_size=200,
        target_seconds=10.0,
        optimize_by_rows_per_second=True,
        target_rows_per_second_window=1,
        target_rows_per_second_deadband=0.1,
    )

    sizer.update(1.0, inserted_rows=100)
    sizer.update(0.9, inserted_rows=90)
    sizer.update(1.1, inserted_rows=110)

    assert sizer.current_size == 121
    assert sizer.baseline_size == 110
    assert sizer.baseline_rows_per_second == pytest.approx(100.0)
    assert sizer.probe_direction == "grow"
    assert sizer.is_experimental_size is True


def test_adaptive_batch_sizer_grow_worse_rolls_back_to_last_good_size() -> None:
    sizer = models_module.AdaptiveBatchSizer(
        enabled=True,
        current_size=100,
        min_size=1,
        max_size=200,
        target_seconds=10.0,
        optimize_by_rows_per_second=True,
        target_rows_per_second_window=1,
        target_rows_per_second_deadband=0.1,
    )

    sizer.update(1.0, inserted_rows=100)
    sizer.update(0.9, inserted_rows=90)
    sizer.update(1.1, inserted_rows=110)
    sizer.update(121 / 80, inserted_rows=121)

    assert sizer.current_size == 110
    assert sizer.baseline_size == 110
    assert sizer.baseline_rows_per_second == pytest.approx(100.0)
    assert sizer.probe_direction is None
    assert sizer.is_experimental_size is False


def test_adaptive_batch_sizer_shrink_better_accepts_smaller_size() -> None:
    sizer = models_module.AdaptiveBatchSizer(
        enabled=True,
        current_size=100,
        min_size=1,
        max_size=200,
        target_seconds=10.0,
        optimize_by_rows_per_second=True,
        target_rows_per_second_window=1,
        target_rows_per_second_deadband=0.1,
    )

    sizer.update(1.0, inserted_rows=100)
    sizer.update(0.75, inserted_rows=90)

    assert sizer.current_size == 81
    assert sizer.baseline_size == 90
    assert sizer.baseline_rows_per_second == 120.0
    assert sizer.probe_direction == "shrink"
    assert sizer.is_experimental_size is True


def test_adaptive_batch_sizer_rows_per_second_respects_caps_and_small_deltas() -> None:
    small = models_module.AdaptiveBatchSizer(
        enabled=True,
        current_size=5,
        min_size=1,
        max_size=10,
        target_seconds=10.0,
        optimize_by_rows_per_second=True,
        adaptive_batch_size_step=0.1,
    )
    small.update(1.0, inserted_rows=5)
    assert small.current_size == 4

    min_capped = models_module.AdaptiveBatchSizer(
        enabled=True,
        current_size=10,
        min_size=10,
        max_size=20,
        target_seconds=10.0,
        optimize_by_rows_per_second=True,
        adaptive_batch_size_step=0.5,
    )
    min_capped.update(1.0, inserted_rows=10)
    assert min_capped.current_size == 10
    assert min_capped.is_experimental_size is False
    assert min_capped.noop_probe_size == 10
    assert min_capped.noop_probe_direction == "shrink"
    min_capped.update(1.0, inserted_rows=10)
    assert min_capped.current_size == 10
    assert min_capped.is_experimental_size is False

    max_capped = models_module.AdaptiveBatchSizer(
        enabled=True,
        current_size=10,
        min_size=9,
        max_size=10,
        target_seconds=10.0,
        optimize_by_rows_per_second=True,
        target_rows_per_second_window=1,
        adaptive_batch_size_step=0.5,
    )
    max_capped.update(1.0, inserted_rows=10)
    max_capped.update(0.9, inserted_rows=9)
    assert max_capped.current_size == 10
    assert max_capped.is_experimental_size is False
    assert max_capped.noop_probe_size == 10
    assert max_capped.noop_probe_direction == "grow"


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
    assert options.adaptive_batch_size_step == 0.1
    assert options.target_rows_per_second_window == 5
    assert options.target_rows_per_second_deadband == 0.15

    custom_window_options = transfer_api_module.build_transfer_options(
        from_db="gp",
        to_db="trino",
        from_sql="select id from source_table",
        to_table="sandbox.target",
        adaptive_batch_size_step=0.25,
        target_rows_per_second_window=3,
        target_rows_per_second_deadband=0.05,
    )
    assert custom_window_options.adaptive_batch_size_step == 0.25
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


@pytest.mark.parametrize("gp_insert_chunk_size", [0, -1])
def test_transfer_options_rejects_invalid_gp_insert_chunk_size(
    gp_insert_chunk_size: int,
) -> None:
    with pytest.raises(
        ValueError,
        match="gp_insert_chunk_size must be a positive integer",
    ):
        transfer_api_module.build_transfer_options(
            from_db="trino",
            to_db="gp",
            from_sql="select id from source_table",
            to_table="sandbox.target",
            gp_insert_chunk_size=gp_insert_chunk_size,
        )


def test_transfer_options_rejects_gp_insert_chunk_size_for_non_gp_target() -> None:
    with pytest.raises(
        ValueError,
        match="gp_insert_chunk_size can only be used when to_db has type 'gp'",
    ):
        transfer_api_module.build_transfer_options(
            from_db="gp",
            to_db="trino",
            from_sql="select id from source_table",
            to_table="sandbox.target",
            gp_insert_chunk_size=10_000,
        )


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


@pytest.mark.parametrize(
    "adaptive_batch_size_step",
    [0, -0.1, 1, 1.1, float("nan"), float("inf"), True, "0.1"],
)
def test_transfer_options_validate_adaptive_batch_size_step(
    adaptive_batch_size_step: Any,
) -> None:
    with pytest.raises(ValueError, match="adaptive_batch_size_step"):
        transfer_api_module.build_transfer_options(
            from_db="gp",
            to_db="trino",
            from_sql="select id from source_table",
            to_table="sandbox.target",
            adaptive_batch_size_step=adaptive_batch_size_step,
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
        gp_insert_chunk_size=50_000,
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
        assert kwargs["gp_insert_chunk_size"] == 50_000
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


def test_load_stage_batches_uses_configured_step_for_transfer_and_gp_sizers(
    monkeypatch,
) -> None:
    source = RecordingSourceConnection(rows=[(row_id,) for row_id in range(180)])
    connection_refs = models_module.TransferConnectionRefs(
        source={"connection": source},
        target={"connection": object()},
    )
    stage_state = models_module.TransferStageState(
        target_exists=False,
        stage_column_types={"id": "INTEGER"},
    )
    options = models_module.TransferOptions(
        from_db_key="trino",
        from_db_backend="trino",
        to_db_key="gp_sandbox",
        to_db_backend="gp",
        source_sql="select id from source_table",
        target_table="sandbox.target",
        batch_size=100,
        adaptive_batch_size=True,
        min_batch_size=1,
        max_batch_size=200,
        adaptive_batch_size_step=0.2,
        gp_insert_chunk_size=2_000,
    )
    observed_page_sizes: list[int] = []

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
        observed_page_sizes.append(kwargs["gp_insert_page_size_getter"]())
        kwargs["on_gp_insert_page_success"](1.0, 2_000)
        observed_page_sizes.append(kwargs["gp_insert_page_size_getter"]())
        kwargs["on_success"](1.0, len(rows))
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

    assert total_rows == 180
    assert source.cursor_obj.fetch_sizes[:2] == [100, 80]
    assert observed_page_sizes[:2] == [2_000, 1_600]


def test_load_stage_batches_starts_adaptive_gp_insert_pages_at_default(
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
        from_db_key="trino",
        from_db_backend="trino",
        to_db_key="gp_sandbox",
        to_db_backend="gp",
        source_sql="select id from source_table",
        target_table="sandbox.target",
        batch_size=2,
        adaptive_batch_size=True,
    )
    observed_page_sizes: list[int] = []

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
        observed_page_sizes.append(kwargs["gp_insert_page_size_getter"]())
        kwargs["on_gp_insert_page_success"](1.0, len(rows))
        kwargs["on_success"](1.0, len(rows))
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

    assert total_rows == 2
    assert observed_page_sizes == [10_000]


def test_load_stage_batches_starts_adaptive_gp_insert_pages_at_explicit_size(
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
        from_db_key="trino",
        from_db_backend="trino",
        to_db_key="gp_sandbox",
        to_db_backend="gp",
        source_sql="select id from source_table",
        target_table="sandbox.target",
        batch_size=2,
        adaptive_batch_size=True,
        gp_insert_chunk_size=100_000,
    )
    observed_page_sizes: list[int] = []

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
        observed_page_sizes.append(kwargs["gp_insert_page_size_getter"]())
        kwargs["on_gp_insert_page_success"](1.0, len(rows))
        kwargs["on_success"](1.0, len(rows))
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

    assert total_rows == 2
    assert observed_page_sizes == [100_000]


def test_load_stage_batches_keeps_gp_insert_pages_fixed_when_adaptive_disabled(
    monkeypatch,
) -> None:
    source = RecordingSourceConnection(rows=[(1,), (2,), (3,), (4,)])
    connection_refs = models_module.TransferConnectionRefs(
        source={"connection": source},
        target={"connection": object()},
    )
    stage_state = models_module.TransferStageState(
        target_exists=False,
        stage_column_types={"id": "INTEGER"},
    )
    options = models_module.TransferOptions(
        from_db_key="trino",
        from_db_backend="trino",
        to_db_key="gp_sandbox",
        to_db_backend="gp",
        source_sql="select id from source_table",
        target_table="sandbox.target",
        batch_size=2,
        adaptive_batch_size=False,
        gp_insert_chunk_size=100_000,
    )
    observed_page_sizes: list[int] = []

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
        observed_page_sizes.append(kwargs["gp_insert_page_size_getter"]())
        kwargs["on_gp_insert_page_success"](0.1, len(rows))
        observed_page_sizes.append(kwargs["gp_insert_page_size_getter"]())
        kwargs["on_success"](1.0, len(rows))
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

    assert total_rows == 4
    assert observed_page_sizes == [100_000, 100_000, 100_000, 100_000]


def test_load_stage_batches_skips_gp_insert_page_sizer_for_non_gp_target(
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
        to_db_key="trino",
        to_db_backend="trino",
        source_sql="select id from source_table",
        target_table="sandbox.target",
        batch_size=2,
        adaptive_batch_size=True,
    )

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
        assert kwargs["gp_insert_page_size_getter"] is None
        assert kwargs["on_gp_insert_page_success"] is None
        kwargs["on_success"](1.0, len(rows))
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

    assert total_rows == 2


def test_load_stage_batches_uses_parquet_writer_for_trino_fast_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = RecordingSourceConnection(rows=[(1,), (2,), (3,)])
    connection_refs = models_module.TransferConnectionRefs(
        source={"connection": source},
        target={"connection": object()},
    )
    stage_state = models_module.TransferStageState(
        target_exists=False,
        stage_column_types={"id": "BIGINT"},
    )
    options = models_module.TransferOptions(
        from_db_key="gp",
        from_db_backend="gp",
        to_db_key="trino",
        to_db_backend="trino",
        source_sql="select id from source_table",
        target_table="sandbox.target",
        batch_size=2,
        adaptive_batch_size=False,
        transfer_staging_schema="object_storage.sandbox",
        transfer_staging_location="s3://bucket/tmp/analytics_toolkit_transfer",
        transfer_staging_username="target_user",
        trino_mode="parquet",
    )
    written_batches: list[dict[str, Any]] = []

    monkeypatch.setattr(
        attempt_module,
        "ensure_parquet_staging_dependencies",
        lambda: ("pa", "pq", "fsspec"),
    )

    def fake_create_parquet_stage_table(
        options: Any,
        connection_refs: Any,
        stage_state: Any,
    ) -> None:
        del options, connection_refs
        stage_state.first_non_empty_batch = pd.DataFrame({"id": [1]})
        stage_state.stage_table = "object_storage.sandbox.target__stage__abcd1234"
        stage_state.stage_external_location = (
            "s3://bucket/tmp/analytics_toolkit_transfer/target/"
            "__analytics_toolkit_target_user__stage__abcd1234/"
        )
        stage_state.stage_table_created = True

    def fake_write_batch_to_parquet_stage(batch: Any, **kwargs: Any) -> int:
        written_batches.append(
            {
                "rows": list(batch.rows),
                "file_index": kwargs["file_index"],
                "location": kwargs["stage_external_location"],
                "row_group_size": kwargs["row_group_size"],
                "pa": kwargs["pa"],
                "pq": kwargs["pq"],
                "fsspec": kwargs["fsspec_module"],
            }
        )
        return len(batch.rows)

    def fail_insert_rows_batch(*_args: Any, **_kwargs: Any) -> int:
        raise AssertionError("Parquet staging must not call insert_rows_batch")

    monkeypatch.setattr(
        attempt_module,
        "create_parquet_stage_table",
        fake_create_parquet_stage_table,
    )
    monkeypatch.setattr(
        attempt_module,
        "write_batch_to_parquet_stage",
        fake_write_batch_to_parquet_stage,
    )
    monkeypatch.setattr(attempt_module, "insert_rows_batch", fail_insert_rows_batch)

    total_rows = attempt_module.load_stage_batches(
        options=options,
        connection_refs=connection_refs,
        stage_state=stage_state,
        read_retry_cnt=1,
        insert_retry_cnt=1,
    )

    assert total_rows == 3
    assert [batch["rows"] for batch in written_batches] == [[(1,), (2,)], [(3,)]]
    assert [batch["file_index"] for batch in written_batches] == [0, 1]
    assert all(batch["row_group_size"] == 2 for batch in written_batches)
    assert source.cursor_obj.fetch_sizes == [2, 2, 2]


def test_keyed_parquet_writer_includes_slice_and_part_in_filename() -> None:
    batch = models_module.RowBatch(columns=["id"], rows=[(1,)])
    opened_uris: list[str] = []

    class FakeTable:
        @staticmethod
        def from_pydict(values: dict[str, list[Any]]) -> dict[str, list[Any]]:
            return values

    class FakePq:
        @staticmethod
        def write_table(
            _arrow_table: Any,
            spooled_file: Any,
            *,
            row_group_size: int,
        ) -> None:
            del row_group_size
            spooled_file.write(b"parquet")

    class FakeFsspec:
        def open(self, uri: str, mode: str) -> io.BytesIO:
            assert mode == "wb"
            opened_uris.append(uri)
            return io.BytesIO()

    inserted_rows = parquet_stage_module.write_batch_to_parquet_stage(
        batch,
        file_index=7,
        slice_index=3,
        stage_external_location="s3://bucket/tmp/stage/",
        pa=SimpleNamespace(Table=FakeTable),
        pq=FakePq,
        fsspec_module=FakeFsspec(),
        row_group_size=100,
    )

    assert inserted_rows == 1
    assert opened_uris == ["s3://bucket/tmp/stage/slice-00003-part-00007.parquet"]


def test_load_parquet_stage_infers_schema_from_first_row_group(
    monkeypatch: pytest.MonkeyPatch,
    ) -> None:
    source = RecordingSourceConnection(rows=[(1, "a")])
    source.cursor_obj.description = [
        ("id", 23, None, None, None, None),
        ("label", 25, None, None, None, None),
    ]
    connection_refs = models_module.TransferConnectionRefs(
        source={"connection": source},
        target={"connection": object()},
    )
    stage_state = models_module.TransferStageState(target_exists=False)
    options = models_module.TransferOptions(
        from_db_key="gp",
        from_db_backend="gp",
        to_db_key="trino",
        to_db_backend="trino",
        source_sql="select id, label from source_table",
        target_table="sandbox.target",
        batch_size=1,
        transfer_staging_schema="object_storage.sandbox",
        transfer_staging_location="s3://bucket/tmp/analytics_toolkit_transfer",
        transfer_staging_username="target_user",
        trino_mode="parquet",
    )

    monkeypatch.setattr(
        attempt_module,
        "ensure_parquet_staging_dependencies",
        lambda: ("pa", "pq", "fsspec"),
    )
    monkeypatch.setattr(
        attempt_module,
        "write_batch_to_parquet_stage",
        lambda batch, **kwargs: len(batch.rows),
    )

    def fake_create_parquet_stage_table(
        options: Any,
        connection_refs: Any,
        stage_state: Any,
    ) -> None:
        del options, connection_refs
        stage_state.stage_table = "object_storage.sandbox.target__stage__abcd1234"
        stage_state.stage_external_location = "s3://bucket/tmp/stage/"
        stage_state.stage_table_created = True

    monkeypatch.setattr(
        attempt_module,
        "create_parquet_stage_table",
        fake_create_parquet_stage_table,
    )

    total_rows = attempt_module.load_stage_batches(
        options=options,
        connection_refs=connection_refs,
        stage_state=stage_state,
        read_retry_cnt=1,
        insert_retry_cnt=1,
    )

    assert total_rows == 1
    assert stage_state.stage_column_types == {"id": "BIGINT", "label": "VARCHAR"}
    assert list(stage_state.first_non_empty_batch.columns) == ["id", "label"]


def test_create_parquet_stage_table_uses_staging_schema_and_location(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executed_sqls: list[str] = []

    class FakeCursor:
        def execute(self, sql: str) -> None:
            executed_sqls.append(sql)

        def close(self) -> None:
            pass

    class FakeConnection:
        def cursor(self) -> FakeCursor:
            return FakeCursor()

    options = models_module.TransferOptions(
        from_db_key="gp",
        from_db_backend="gp",
        to_db_key="trino",
        to_db_backend="trino",
        source_sql="select id from source_table",
        target_table="sandbox.target",
        transfer_staging_schema="object_storage.sandbox",
        transfer_staging_location="s3://bucket/tmp/analytics_toolkit_transfer",
        transfer_staging_username="target_user",
        trino_mode="parquet",
    )
    stage_state = models_module.TransferStageState(
        target_exists=False,
        stage_column_types={"id": "BIGINT"},
    )

    monkeypatch.setattr(
        parquet_stage_module,
        "table_exists",
        lambda *args, **kwargs: False,
    )
    monkeypatch.setattr(
        parquet_stage_module.uuid,
        "uuid4",
        lambda: SimpleNamespace(hex="abcd1234"),
    )

    parquet_stage_module.create_parquet_stage_table(
        options=options,
        connection_refs=models_module.TransferConnectionRefs(
            target={"connection": FakeConnection()},
        ),
        stage_state=stage_state,
    )

    assert stage_state.stage_table == (
        "object_storage.sandbox.target__analytics_toolkit_target_user__stage__abcd1234"
    )
    assert stage_state.stage_external_location == (
        "s3://bucket/tmp/analytics_toolkit_transfer/target/"
        "__analytics_toolkit_target_user__stage__abcd1234/"
    )
    assert executed_sqls == [
        "CREATE TABLE "
        "object_storage.sandbox.target__analytics_toolkit_target_user__stage__abcd1234 "
        "(\"id\" BIGINT) WITH (format = 'PARQUET', "
        "external_location = 's3://bucket/tmp/analytics_toolkit_transfer/target/"
        "__analytics_toolkit_target_user__stage__abcd1234/')"
    ]


def test_cleanup_stage_drops_stage_table_and_removes_remote_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dropped: list[str | None] = []
    removed: list[str] = []
    stage_state = models_module.TransferStageState(
        target_exists=False,
        stage_table_created=True,
        stage_table="object_storage.sandbox.target__stage__abcd1234",
        stage_external_location="s3://bucket/tmp/stage/",
    )
    options = make_progress_options(
        to_db_key="trino",
        to_db_backend="trino",
        transfer_staging_schema="object_storage.sandbox",
        transfer_staging_location="s3://bucket/tmp",
        trino_mode="parquet",
    )

    monkeypatch.setattr(
        finalize_module,
        "cleanup_stage_table_with_retry",
        lambda connection_type,
        connection_key,
        connection_ref,
        stage_table,
        **kwargs: dropped.append(stage_table),
    )
    monkeypatch.setattr(
        finalize_module,
        "cleanup_parquet_stage_location",
        lambda stage_external_location: removed.append(stage_external_location),
    )

    finalize_module.cleanup_stage(
        options=options,
        connection_refs=models_module.TransferConnectionRefs(
            target={"connection": object()},
        ),
        stage_state=stage_state,
        read_retry_cnt=1,
    )

    assert dropped == ["object_storage.sandbox.target__stage__abcd1234"]
    assert removed == ["s3://bucket/tmp/stage/"]


def test_parquet_staging_missing_dependencies_raise_clear_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = make_progress_options(
        to_db_key="trino",
        to_db_backend="trino",
        transfer_staging_schema="object_storage.sandbox",
        transfer_staging_location="s3://bucket/tmp/analytics_toolkit_transfer",
        trino_mode="parquet",
    )

    monkeypatch.setattr(
        attempt_module,
        "ensure_parquet_staging_dependencies",
        lambda: (_ for _ in ()).throw(
            ImportError(parquet_stage_module.PARQUET_STAGING_IMPORT_ERROR)
        ),
    )

    with pytest.raises(ImportError, match="pyarrow, fsspec, and s3fs"):
        attempt_module.load_stage_batches(
            options=options,
            connection_refs=models_module.TransferConnectionRefs(
                source={"connection": RecordingSourceConnection(rows=[(1,)])},
                target={"connection": object()},
            ),
            stage_state=models_module.TransferStageState(
                target_exists=False,
                stage_column_types={"id": "BIGINT"},
            ),
            read_retry_cnt=1,
            insert_retry_cnt=1,
        )


def test_write_batch_to_parquet_stage_uses_one_spooled_file_without_getvalue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active_spooled_files = 0
    max_active_spooled_files = 0
    uploads: list[tuple[str, Any]] = []

    class FakeSpooledFile:
        _rolled = False

        def __init__(self, max_size: int) -> None:
            nonlocal active_spooled_files, max_active_spooled_files
            assert max_size == parquet_stage_module.PARQUET_STAGE_MAX_SPOOL_BYTES
            active_spooled_files += 1
            max_active_spooled_files = max(
                max_active_spooled_files,
                active_spooled_files,
            )
            self.closed = False
            self.position = 0

        def seek(self, position: int) -> None:
            self.position = position

        def close(self) -> None:
            nonlocal active_spooled_files
            self.closed = True
            active_spooled_files -= 1

        def getvalue(self) -> bytes:
            raise AssertionError("Parquet staging must not materialize file bytes")

    monkeypatch.setattr(
        parquet_stage_module.tempfile,
        "SpooledTemporaryFile",
        FakeSpooledFile,
    )
    monkeypatch.setattr(
        parquet_stage_module,
        "row_batch_to_arrow_table",
        lambda pa, batch: {"rows": list(batch.rows)},
    )
    monkeypatch.setattr(
        parquet_stage_module,
        "write_arrow_table_to_parquet",
        lambda pq, arrow_table, spooled_file, row_group_size: None,
    )
    monkeypatch.setattr(
        parquet_stage_module,
        "upload_spooled_file",
        lambda fsspec_module, spooled_file, remote_uri: uploads.append(
            (remote_uri, spooled_file)
        ),
    )

    row_count = parquet_stage_module.write_batch_to_parquet_stage(
        models_module.RowBatch(columns=["id"], rows=[(1,), (2,)]),
        file_index=0,
        stage_external_location="s3://bucket/tmp/stage/",
        pa=object(),
        pq=object(),
        fsspec_module=object(),
        row_group_size=2,
    )

    assert row_count == 2
    assert max_active_spooled_files == 1
    assert active_spooled_files == 0
    assert uploads[0][0] == "s3://bucket/tmp/stage/part-00000.parquet"
    assert uploads[0][1].closed is True


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
    assert plan.options["adaptive_batch_size_step"] == 0.1
    assert plan.options["target_rows_per_second_window"] == 5
    assert plan.options["target_rows_per_second_deadband"] == 0.15
    assert plan.options["target_batch_memory_mb"] == 32.0
    assert plan.options["max_batch_size"] is None
    assert plan.options["gp_insert_chunk_size"] is None
    assert plan.options["adaptive_gp_insert_chunk_size"] is False
    assert plan.options["initial_gp_insert_chunk_size"] is None

    plan = transfer_api_module.transfer_table(
        from_db="trino",
        to_db="gp",
        from_sql="select id from source_table",
        to_table="sandbox.target",
        dry_run=True,
        target_rows_per_second_window=3,
        target_rows_per_second_deadband=0.05,
        adaptive_batch_size_step=0.25,
        gp_insert_chunk_size=50_000,
    )
    assert plan.options["target_rows_per_second_window"] == 3
    assert plan.options["target_rows_per_second_deadband"] == 0.05
    assert plan.options["adaptive_batch_size_step"] == 0.25
    assert plan.options["gp_insert_chunk_size"] == 50_000
    assert plan.options["adaptive_gp_insert_chunk_size"] is True
    assert plan.options["initial_gp_insert_chunk_size"] == 50_000

    plan = transfer_api_module.transfer_table(
        from_db="trino",
        to_db="gp",
        from_sql="select id from source_table",
        to_table="sandbox.target",
        dry_run=True,
    )
    assert plan.options["gp_insert_chunk_size"] is None
    assert plan.options["adaptive_gp_insert_chunk_size"] is True
    assert plan.options["initial_gp_insert_chunk_size"] == 10_000


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
