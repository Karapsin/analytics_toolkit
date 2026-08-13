from __future__ import annotations

import importlib
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.sql_fakes import FakeDbapiConnection

CURRENT_DT = date.today().strftime("%Y%m%d")
TEST_CH_TABLE = f"test_table_{CURRENT_DT}"
TEST_CH_SHARD_TABLE = f"test_table_{CURRENT_DT}_shard"
TEST_CH_STAGE_TABLE = f"test_table_{CURRENT_DT}__stage__abcd1234"
TEST_CH_SHARD_RELATION = f"test_table_{CURRENT_DT}_shard"

create_sql_table_module = importlib.import_module("analytics_toolkit.sql.ddl.api")
ch_wait_module = importlib.import_module("analytics_toolkit.sql.backends.ch.wait")
load_sql_table_module = importlib.import_module("analytics_toolkit.sql.dml.load.load_sql_table")
gp_insert_module = importlib.import_module("analytics_toolkit.sql.backends.gp.insert")
load_df_module = importlib.import_module("analytics_toolkit.sql.dml.load.load_df")
parquet_stage_module = importlib.import_module(
    "analytics_toolkit.sql.dml.transfer.flow.parquet_stage"
)
table_ops_module = importlib.import_module("analytics_toolkit.sql.dml.table.write_modes")


def _write_trino_connections(
    write_sql_connections: Any,
    *,
    s3_transfer_staging_location: str | None,
    s3_transfer_staging_schema: str | None = "hive.pa_core_stage",
    ddl_defaults: dict[str, object] | None = None,
) -> None:
    config: dict[str, object] = {
        "type": "trino",
        "host": "trino.example",
        "port": 8080,
        "user": "target_user",
        "password": "password",
        "catalog": "iceberg",
        "schema": "sandbox",
        "transfer_staging_schema": "object_storage.pa_core_stage",
        "upsert_partition_drop_sql_template": (
            "ALTER TABLE {table} DROP PARTITION ({partition_column} = {partition_value})"
        ),
    }
    if s3_transfer_staging_location is not None:
        config["s3_transfer_staging_location"] = s3_transfer_staging_location
    if s3_transfer_staging_location is not None and s3_transfer_staging_schema is not None:
        config["s3_transfer_staging_schema"] = s3_transfer_staging_schema
    if ddl_defaults is not None:
        config["ddl_defaults"] = ddl_defaults
    write_sql_connections({"trino_stage": config})


class FakeClickHouseClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.commands: list[str] = []
        self.queries: list[str] = []
        self.created_tables: set[str] = set()
        self.close_calls = 0

    def command(self, sql: str) -> None:
        self.commands.append(sql)
        self._track_table_ddl(sql)

    def query(self, sql: str) -> object:
        self.queries.append(sql)
        if sql.startswith("SELECT getMacro("):
            return type("FakeResult", (), {"result_rows": [("core",)]})()
        if "clusterAllReplicas" in sql and "system, one" in sql:
            return type("FakeResult", (), {"result_rows": [(1,)]})()
        if "FROM system.clusters" in sql:
            return type("FakeResult", (), {"result_rows": [(1,)]})()
        if "clusterAllReplicas" in sql and "system, tables" in sql:
            return type(
                "FakeResult",
                (),
                {"result_rows": [(self._cluster_table_count(sql),)]},
            )()
        if "clusterAllReplicas" in sql and "system, columns" in sql:
            return type(
                "FakeResult",
                (),
                {"result_rows": [(sql.count("name = ") or 1,)]},
            )()
        if sql.startswith("EXISTS TABLE "):
            table_name = sql[len("EXISTS TABLE ") :].strip()
            return type(
                "FakeResult",
                (),
                {"result_rows": [(int(table_name in self.created_tables),)]},
            )()
        return type("FakeResult", (), {"result_rows": [(1,)]})()

    def insert_df(
        self,
        table: str,
        df: pd.DataFrame,
        column_names: list[str],
    ) -> None:
        self.calls.append(
            {
                "table": table,
                "df": df.copy(),
                "column_names": list(column_names),
            }
        )

    def insert(
        self,
        table: str,
        data: list[tuple[object, ...]],
        column_names: list[str],
        column_type_names: list[str] | None = None,
    ) -> None:
        self.calls.append(
            {
                "table": table,
                "data": list(data),
                "column_names": list(column_names),
                "column_type_names": (
                    list(column_type_names) if column_type_names is not None else None
                ),
            }
        )

    def close(self) -> None:
        self.close_calls += 1

    def _track_table_ddl(self, sql: str) -> None:
        if sql.startswith("CREATE TABLE IF NOT EXISTS "):
            table_name = sql[len("CREATE TABLE IF NOT EXISTS ") :].split(maxsplit=1)[0]
            self.created_tables.add(table_name)
            return
        if sql.startswith("CREATE TABLE "):
            table_name = sql[len("CREATE TABLE ") :].split(maxsplit=1)[0]
            self.created_tables.add(table_name)
            return
        if sql.startswith("DROP TABLE IF EXISTS "):
            table_name = sql[len("DROP TABLE IF EXISTS ") :].split(maxsplit=1)[0]
            self.created_tables.discard(table_name)

    def _cluster_table_count(self, sql: str) -> int:
        marker = "AND name = '"
        if marker not in sql:
            return len(self.created_tables)
        relation_name = sql.split(marker, 1)[1].split("'", 1)[0]
        return sum(
            1
            for table_name in self.created_tables
            if table_name.rsplit(".", 1)[-1] == relation_name
        )


def test_insert_table_batch_normalizes_decimal_for_clickhouse() -> None:
    client = FakeClickHouseClient()
    connection_ref = {"connection": client}
    batch = pd.DataFrame(
        {
            "amount": [Decimal("1.20"), None],
            "label": ["ok", None],
            "count": [1, 2],
        }
    )

    inserted_rows = load_sql_table_module.insert_table_batch(
        connection_type="ch",
        connection_ref=connection_ref,
        table_name="schema.stage_table",
        batch=batch,
        retry_fn=lambda **kwargs: kwargs["operation"](1),
        retry_cnt=1,
        timeout_increment=0,
    )

    assert inserted_rows == 2
    assert len(client.calls) == 1

    inserted_df = client.calls[0]["df"]
    assert isinstance(inserted_df, pd.DataFrame)
    assert inserted_df["amount"].tolist() == [1.2, None]
    assert inserted_df["label"].tolist() == ["ok", None]
    assert inserted_df["count"].tolist() == [1, 2]


def test_load_df_updates_progress_bar(monkeypatch) -> None:
    client = FakeClickHouseClient()
    progress_bars: list[object] = []
    batch = pd.DataFrame({"id": [1, 2, 3], "value": ["a", "b", "c"]})

    class FakeTqdm:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs
            self.updates: list[int] = []
            self.closed = False
            progress_bars.append(self)

        def update(self, value: int) -> None:
            self.updates.append(value)

        def close(self) -> None:
            self.closed = True

    def fake_insert_table_batch(*args, **kwargs) -> int:
        df = args[3]
        kwargs["on_progress"](len(df))
        return len(df)

    monkeypatch.setattr(load_df_module, "tqdm", FakeTqdm)
    monkeypatch.setattr(load_df_module, "get_sql_connection", lambda key: client)
    monkeypatch.setattr(load_df_module, "table_exists", lambda *args, **kwargs: False)
    monkeypatch.setattr(
        load_df_module,
        "_create_sql_table_with_connection",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(load_df_module, "insert_table_batch", fake_insert_table_batch)
    monkeypatch.setattr(load_df_module, "analyze_table", lambda *args, **kwargs: None)

    inserted_rows = load_df_module.load_df(
        "ch",
        TEST_CH_TABLE,
        batch,
        retry_cnt=1,
        timeout_increment=0,
        progress=True,
    )

    assert inserted_rows == 3
    assert len(progress_bars) == 1
    progress_bar = progress_bars[0]
    assert progress_bar.kwargs == {
        "total": 3,
        "desc": f"load_df ch.{TEST_CH_TABLE}",
        "unit": "row",
        "disable": False,
    }
    assert progress_bar.updates == [3]
    assert progress_bar.closed is True


def test_load_df_progress_false_disables_bar(monkeypatch) -> None:
    client = FakeClickHouseClient()
    progress_bars: list[object] = []
    batch = pd.DataFrame({"id": [1, 2]})

    class FakeTqdm:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs
            self.updates: list[int] = []
            self.closed = False
            progress_bars.append(self)

        def update(self, value: int) -> None:
            self.updates.append(value)

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(load_df_module, "tqdm", FakeTqdm)
    monkeypatch.setattr(load_df_module, "get_sql_connection", lambda key: client)
    monkeypatch.setattr(load_df_module, "table_exists", lambda *args, **kwargs: False)
    monkeypatch.setattr(
        load_df_module,
        "_create_sql_table_with_connection",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(load_df_module, "insert_table_batch", lambda *args, **kwargs: 2)
    monkeypatch.setattr(load_df_module, "analyze_table", lambda *args, **kwargs: None)

    inserted_rows = load_df_module.load_df(
        "ch",
        TEST_CH_TABLE,
        batch,
        retry_cnt=1,
        timeout_increment=0,
        progress=False,
    )

    assert inserted_rows == 2
    assert len(progress_bars) == 1
    assert progress_bars[0].kwargs["disable"] is True
    assert progress_bars[0].updates == [2]
    assert progress_bars[0].closed is True


def test_load_df_dry_run_does_not_create_progress_bar(monkeypatch) -> None:
    progress_bars: list[object] = []

    class FakeTqdm:
        def __init__(self, **kwargs: object) -> None:
            progress_bars.append(self)

        def update(self, value: int) -> None:
            pass

        def close(self) -> None:
            pass

    monkeypatch.setattr(load_df_module, "tqdm", FakeTqdm)

    plan = load_df_module.load_df(
        "gp",
        "sandbox.target",
        pd.DataFrame({"id": [1]}),
        dry_run=True,
    )

    assert plan.operation == "load_df"
    assert progress_bars == []


def test_build_load_options_accepts_scalar_key_columns() -> None:
    options = load_df_module._build_load_options(
        db_key="gp",
        destination_table="sandbox.target",
        append=False,
        write_mode="upsert",
        gp_distributed_by_key=" id ",
        key_columns=" id ",
        trino_insert_chunk_size=None,
    )

    assert options.gp_distributed_by_key == ["id"]
    assert options.key_columns == ["id"]


@pytest.mark.parametrize("progress", [None, 0, 1, "yes"])
def test_load_df_validates_progress(progress: object) -> None:
    with pytest.raises(ValueError, match="progress"):
        load_df_module.load_df(
            "gp",
            "sandbox.target",
            pd.DataFrame({"id": [1]}),
            dry_run=True,
            progress=progress,
        )


def test_load_df_drops_overlap_stage_table_after_success(monkeypatch) -> None:
    class FakeConnection:
        def __init__(self) -> None:
            self.close_calls = 0

        def close(self) -> None:
            self.close_calls += 1

    connection = FakeConnection()
    config = SimpleNamespace(
        connection_key="gp",
        backend="gp",
        user="target_user",
        transfer_staging_schema="transfer_schema",
        insert_chunk_size=None,
    )
    cleanups: list[tuple[str, str, str]] = []

    monkeypatch.setattr(load_df_module, "get_connection_config", lambda key: config)
    monkeypatch.setattr(load_df_module, "get_sql_connection", lambda key: connection)
    monkeypatch.setattr(load_df_module, "table_exists", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        load_df_module,
        "_create_sql_table_with_connection",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        load_df_module,
        "create_stage_table",
        lambda **kwargs: f"{kwargs['target_table']}__stage__ok",
    )
    monkeypatch.setattr(
        load_df_module, "validate_stage_target_key_overlap", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(load_df_module, "insert_from_table", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        load_df_module,
        "insert_table_batch",
        lambda connection_type, connection_ref, table_name, batch, **kwargs: len(batch),
    )
    monkeypatch.setattr(load_df_module, "analyze_table", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        load_df_module,
        "cleanup_stage_table_with_retry",
        lambda connection_type, connection_key, connection_ref, table_name, **kwargs: (
            cleanups.append(
                (connection_type, connection_key, table_name),
            )
        ),
    )

    inserted_rows = load_df_module.load_df(
        "gp",
        "sandbox.target",
        pd.DataFrame({"id": [1, 2], "value": [10, 20]}),
        append=True,
        key_columns=["id"],
        progress=False,
        retry_cnt=1,
        timeout_increment=0,
    )

    assert inserted_rows == 2
    assert cleanups == [("gp", "gp", "sandbox.target__stage__ok")]


def test_load_df_drops_overlap_stage_table_on_error(monkeypatch) -> None:
    class FakeConnection:
        def __init__(self) -> None:
            self.close_calls = 0

        def close(self) -> None:
            self.close_calls += 1

    connection = FakeConnection()
    config = SimpleNamespace(
        connection_key="gp",
        backend="gp",
        user="target_user",
        transfer_staging_schema="transfer_schema",
        insert_chunk_size=None,
    )
    cleanups: list[tuple[str, str, str]] = []

    def fake_insert_table_batch(*args, **kwargs) -> int:
        del args, kwargs
        raise RuntimeError("insert failed")

    monkeypatch.setattr(load_df_module, "get_connection_config", lambda key: config)
    monkeypatch.setattr(load_df_module, "get_sql_connection", lambda key: connection)
    monkeypatch.setattr(load_df_module, "table_exists", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        load_df_module,
        "_create_sql_table_with_connection",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        load_df_module,
        "create_stage_table",
        lambda **kwargs: f"{kwargs['target_table']}__stage__err",
    )
    monkeypatch.setattr(
        load_df_module, "validate_stage_target_key_overlap", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(load_df_module, "insert_from_table", lambda *args, **kwargs: None)
    monkeypatch.setattr(load_df_module, "insert_table_batch", fake_insert_table_batch)
    monkeypatch.setattr(load_df_module, "analyze_table", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        load_df_module,
        "cleanup_stage_table_with_retry",
        lambda connection_type, connection_key, connection_ref, table_name, **kwargs: (
            cleanups.append(
                (connection_type, connection_key, table_name),
            )
        ),
    )

    with pytest.raises(RuntimeError, match="insert failed"):
        load_df_module.load_df(
            "gp",
            "sandbox.target",
            pd.DataFrame({"id": [1, 2], "value": [10, 20]}),
            append=True,
            key_columns=["id"],
            progress=False,
            retry_cnt=1,
            timeout_increment=0,
        )

    assert cleanups == [("gp", "gp", "sandbox.target__stage__err")]


def test_load_df_upsert_empty_existing_target_returns_zero(monkeypatch) -> None:
    connection = FakeDbapiConnection()

    monkeypatch.setattr(load_df_module, "get_sql_connection", lambda key: connection)
    monkeypatch.setattr(load_df_module, "table_exists", lambda *args, **kwargs: True)

    result = load_df_module.load_df(
        "gp",
        "sandbox.target",
        pd.DataFrame({"id": []}),
        write_mode="upsert",
        key_columns=["id"],
        retry_cnt=1,
        timeout_increment=0,
    )

    assert result == 0
    assert connection.executed == []


def test_load_df_upsert_missing_target_creates_and_inserts(monkeypatch) -> None:
    connection = FakeDbapiConnection()
    create_calls: list[str] = []

    monkeypatch.setattr(load_df_module, "get_sql_connection", lambda key: connection)
    monkeypatch.setattr(load_df_module, "table_exists", lambda *args, **kwargs: False)
    monkeypatch.setattr(
        load_df_module,
        "_create_sql_table_with_connection",
        lambda connection_type, connection, table_name, *args, **kwargs: create_calls.append(
            table_name
        ),
    )
    monkeypatch.setattr(load_df_module, "insert_table_batch", lambda *args, **kwargs: 2)
    monkeypatch.setattr(load_df_module, "analyze_table", lambda *args, **kwargs: None)

    result = load_df_module.load_df(
        "gp",
        "sandbox.target",
        pd.DataFrame({"id": [1, 2], "score": [10, 20]}),
        write_mode="upsert",
        key_columns=["id"],
        retry_cnt=1,
        timeout_increment=0,
    )

    assert result == 2
    assert create_calls == ["sandbox.target"]


def test_load_df_upsert_existing_target_cleans_stage_on_finalization_error(
    monkeypatch,
) -> None:
    connection = FakeDbapiConnection()
    cleanups: list[str] = []

    monkeypatch.setattr(load_df_module, "get_sql_connection", lambda key: connection)
    monkeypatch.setattr(load_df_module, "table_exists", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        load_df_module,
        "_create_sql_table_with_connection",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        load_df_module,
        "create_stage_table",
        lambda **kwargs: "sandbox.target__stage__upsert",
    )
    monkeypatch.setattr(load_df_module, "insert_table_batch", lambda *args, **kwargs: 2)
    monkeypatch.setattr(load_df_module, "validate_stage_uniqueness", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        load_df_module,
        "upsert_stage_table",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("merge failed")),
    )
    monkeypatch.setattr(
        load_df_module,
        "cleanup_stage_table_with_retry",
        lambda connection_type, connection_key, connection_ref, table_name, **kwargs: (
            cleanups.append(table_name)
        ),
    )

    with pytest.raises(RuntimeError, match="merge failed"):
        load_df_module.load_df(
            "gp",
            "sandbox.target",
            pd.DataFrame({"id": [1, 2], "score": [10, 20]}),
            write_mode="upsert",
            key_columns=["id"],
            retry_cnt=1,
            timeout_increment=0,
        )

    assert cleanups == ["sandbox.target__stage__upsert"]


def test_load_df_gp_upsert_existing_target_uses_target_types_and_df_columns(
    monkeypatch,
) -> None:
    connection = FakeDbapiConnection()

    monkeypatch.setattr(load_df_module, "get_sql_connection", lambda key: connection)
    monkeypatch.setattr(load_df_module, "table_exists", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        load_df_module,
        "get_table_column_types",
        lambda *args, **kwargs: {
            "score": "INTEGER",
            "id": "BIGINT",
            "extra_col": "TEXT",
        },
    )
    monkeypatch.setattr(
        load_df_module,
        "_create_sql_table_with_connection",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        load_df_module,
        "create_stage_table",
        lambda **kwargs: "sandbox.target__stage__upsert",
    )
    monkeypatch.setattr(load_df_module, "insert_table_batch", lambda *args, **kwargs: 2)
    monkeypatch.setattr(load_df_module, "validate_stage_uniqueness", lambda *args, **kwargs: None)
    monkeypatch.setattr(load_df_module, "analyze_table", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        load_df_module, "cleanup_stage_table_with_retry", lambda *args, **kwargs: None
    )

    result = load_df_module.load_df(
        "gp",
        "sandbox.target",
        pd.DataFrame({"id": [1, 2], "score": [10, 20]}),
        write_mode="upsert",
        key_columns=["id"],
        retry_cnt=1,
        timeout_increment=0,
    )

    assert result == 2
    assert any(
        'INSERT INTO sandbox.target ("id", "score") '
        'SELECT CAST("id" AS BIGINT) AS "id", '
        'CAST("score" AS INTEGER) AS "score" '
        "FROM sandbox.target__stage__upsert" in sql
        for sql in connection.executed
    )


def test_load_df_clickhouse_upsert_existing_target_uses_target_types_and_df_columns(
    monkeypatch,
) -> None:
    client = FakeClickHouseClient()

    monkeypatch.setattr(load_df_module, "get_sql_connection", lambda key: client)
    monkeypatch.setattr(load_df_module, "table_exists", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        load_df_module,
        "get_table_column_types",
        lambda *args, **kwargs: {
            "score": "Int64",
            "id": "UInt64",
            "extra_col": "String",
        },
    )
    monkeypatch.setattr(
        load_df_module,
        "_create_sql_table_with_connection",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        load_df_module,
        "create_stage_table",
        lambda **kwargs: "analytics.target__stage__upsert",
    )
    monkeypatch.setattr(load_df_module, "insert_table_batch", lambda *args, **kwargs: 2)
    monkeypatch.setattr(load_df_module, "validate_stage_uniqueness", lambda *args, **kwargs: None)
    monkeypatch.setattr(load_df_module, "analyze_table", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        load_df_module, "cleanup_stage_table_with_retry", lambda *args, **kwargs: None
    )

    result = load_df_module.load_df(
        "ch",
        "analytics.target",
        pd.DataFrame({"id": [1, 2], "score": [10, 20]}),
        write_mode="upsert",
        key_columns=["id"],
        upsert_partition_column="id",
        retry_cnt=1,
        timeout_increment=0,
    )

    assert result == 2
    assert any(
        "INSERT INTO analytics.target__stage__upsert (`id`, `score`) "
        "SELECT CAST(`id` AS UInt64) AS `id`, CAST(`score` AS Int64) AS `score` "
        "FROM analytics.target__stage__upsert" in sql
        for sql in client.commands
    )


def test_insert_rows_batch_gp_uses_row_tuples_and_normalizes_nulls(monkeypatch) -> None:
    connection = FakeDbapiConnection()
    captured: dict[str, object] = {}

    def fake_execute_values(cursor, sql, rows, page_size):
        captured["sql"] = sql
        captured["rows"] = list(rows)
        captured["page_size"] = page_size

    monkeypatch.setattr(gp_insert_module, "execute_values", fake_execute_values)

    inserted_rows = load_sql_table_module.insert_rows_batch(
        connection_type="gp",
        connection_ref={"connection": connection},
        table_name="schema.stage_table",
        columns=["id", "value"],
        rows=[(1, pd.NA), (2, float("nan"))],
        retry_fn=lambda **kwargs: kwargs["operation"](1),
        retry_cnt=1,
        timeout_increment=0,
    )

    assert inserted_rows == 2
    assert captured["sql"] == 'INSERT INTO schema.stage_table ("id", "value") VALUES %s'
    assert captured["rows"] == [(1, None), (2, None)]
    assert captured["page_size"] == 2
    assert connection.commit_calls == 1


def test_insert_rows_batch_gp_normalizes_uuid_values(monkeypatch) -> None:
    connection = FakeDbapiConnection()
    captured_rows: list[tuple[object, ...]] = []
    uuid_value = UUID("f5d10b74-0409-4f31-bc7c-df82b8688f19")

    def fake_execute_values(cursor, sql, rows, page_size):
        del cursor, sql, page_size
        captured_rows.extend(rows)

    monkeypatch.setattr(gp_insert_module, "execute_values", fake_execute_values)

    inserted_rows = load_sql_table_module.insert_rows_batch(
        connection_type="gp",
        connection_ref={"connection": connection},
        table_name="schema.stage_table",
        columns=["uuid_value"],
        rows=[(uuid_value,)],
        retry_fn=lambda **kwargs: kwargs["operation"](1),
        retry_cnt=1,
        timeout_increment=0,
    )

    assert inserted_rows == 1
    assert captured_rows == [(str(uuid_value),)]


def test_insert_rows_batch_gp_honors_insert_chunk_size(monkeypatch) -> None:
    connection = FakeDbapiConnection()
    captured_calls: list[dict[str, object]] = []
    progress_updates: list[int] = []

    def fake_execute_values(cursor, sql, rows, page_size):
        del cursor, sql
        captured_calls.append(
            {
                "rows": list(rows),
                "page_size": page_size,
            }
        )

    monkeypatch.setattr(gp_insert_module, "execute_values", fake_execute_values)

    inserted_rows = load_sql_table_module.insert_rows_batch(
        connection_type="gp",
        connection_ref={"connection": connection},
        table_name="schema.stage_table",
        columns=["id"],
        rows=[(1,), (2,), (3,)],
        retry_fn=lambda **kwargs: kwargs["operation"](1),
        retry_cnt=1,
        timeout_increment=0,
        gp_insert_chunk_size=2,
        on_progress=progress_updates.append,
    )

    assert inserted_rows == 3
    assert captured_calls == [
        {"rows": [(1,), (2,)], "page_size": 2},
        {"rows": [(3,)], "page_size": 1},
    ]
    assert progress_updates == [2, 1]
    assert connection.commit_calls == 1


def test_insert_gp_rows_can_change_page_size_between_calls(monkeypatch) -> None:
    connection = FakeDbapiConnection()
    captured_calls: list[dict[str, object]] = []
    page_sizes = iter([2, 3, 10])

    def fake_execute_values(cursor, sql, rows, page_size):
        del cursor, sql
        captured_calls.append(
            {
                "rows": list(rows),
                "page_size": page_size,
            }
        )

    monkeypatch.setattr(gp_insert_module, "execute_values", fake_execute_values)

    load_sql_table_module._insert_gp_rows(
        connection=connection,
        table_name="schema.stage_table",
        columns=["id"],
        rows=[(1,), (2,), (3,), (4,), (5,), (6,)],
        page_size_getter=lambda: next(page_sizes),
    )

    assert captured_calls == [
        {"rows": [(1,), (2,)], "page_size": 2},
        {"rows": [(3,), (4,), (5,)], "page_size": 3},
        {"rows": [(6,)], "page_size": 1},
    ]
    assert connection.commit_calls == 1


def test_insert_gp_rows_reports_per_page_success(monkeypatch) -> None:
    connection = FakeDbapiConnection()
    page_successes: list[tuple[float, int]] = []
    progress_updates: list[int] = []
    perf_values = iter([1.0, 1.25, 2.0, 2.75])

    def fake_execute_values(cursor, sql, rows, page_size):
        del cursor, sql, rows, page_size

    monkeypatch.setattr(gp_insert_module, "execute_values", fake_execute_values)
    monkeypatch.setattr(
        gp_insert_module.time,
        "perf_counter",
        lambda: next(perf_values),
    )

    load_sql_table_module._insert_gp_rows(
        connection=connection,
        table_name="schema.stage_table",
        columns=["id"],
        rows=[(1,), (2,), (3,)],
        gp_insert_chunk_size=2,
        on_progress=progress_updates.append,
        on_page_success=lambda duration, rows: page_successes.append((duration, rows)),
    )

    assert progress_updates == [2, 1]
    assert page_successes == [(0.25, 2), (0.75, 1)]
    assert connection.commit_calls == 1


def test_insert_gp_rows_rolls_back_on_error(monkeypatch) -> None:
    connection = FakeDbapiConnection()

    def fake_execute_values(cursor, sql, rows, page_size):
        del cursor, sql, rows, page_size
        raise RuntimeError("insert failed")

    monkeypatch.setattr(gp_insert_module, "execute_values", fake_execute_values)

    with pytest.raises(RuntimeError, match="insert failed"):
        load_sql_table_module._insert_gp_rows(
            connection=connection,
            table_name="schema.stage_table",
            columns=["id"],
            rows=[(1,), (2,)],
            gp_insert_chunk_size=2,
        )

    assert connection.rollback_calls == 1
    assert connection.commit_calls == 0


def test_insert_rows_batch_trino_normalizes_values_and_splits_chunks() -> None:
    connection = FakeDbapiConnection()
    progress_updates: list[int] = []

    inserted_rows = load_sql_table_module.insert_rows_batch(
        connection_type="trino",
        connection_ref={"connection": connection},
        table_name="schema.stage_table",
        columns=["id", "label"],
        rows=[(1.9, "a"), (2, pd.NA), (3, "c")],
        retry_fn=lambda **kwargs: kwargs["operation"](1),
        retry_cnt=1,
        timeout_increment=0,
        target_column_types={"id": "bigint", "label": "varchar"},
        trino_insert_chunk_size=2,
        on_progress=progress_updates.append,
    )

    assert inserted_rows == 3
    assert connection.executed == [
        'INSERT INTO schema.stage_table ("id", "label") VALUES '
        '(CAST(? AS bigint), CAST(? AS varchar)), (CAST(? AS bigint), CAST(? AS varchar))',
        'INSERT INTO schema.stage_table ("id", "label") VALUES '
        '(CAST(? AS bigint), CAST(? AS varchar))',
    ]
    assert connection.executed_params == [
        [1, "a", 2, None],
        [3, "c"],
    ]
    assert progress_updates == [2, 1]


def test_insert_rows_batch_clickhouse_uses_rows_and_column_type_names() -> None:
    client = FakeClickHouseClient()
    progress_updates: list[int] = []

    inserted_rows = load_sql_table_module.insert_rows_batch(
        connection_type="ch",
        connection_ref={"connection": client},
        table_name="schema.stage_table",
        columns=["amount", "label"],
        rows=[(Decimal("1.20"), "ok"), (None, pd.NA)],
        retry_fn=lambda **kwargs: kwargs["operation"](1),
        retry_cnt=1,
        timeout_increment=0,
        target_column_types={
            "amount": "Nullable(Decimal(10, 2))",
            "label": "Nullable(String)",
        },
        on_progress=progress_updates.append,
    )

    assert inserted_rows == 2
    assert progress_updates == [2]
    assert client.calls == [
        {
            "table": "schema.stage_table",
            "data": [(1.2, "ok"), (None, None)],
            "column_names": ["amount", "label"],
            "column_type_names": [
                "Nullable(Decimal(10, 2))",
                "Nullable(String)",
            ],
        }
    ]


def test_batch_insert_sql_builders_preserve_backend_shapes() -> None:
    assert load_sql_table_module.build_gp_batch_insert_sql(
        "schema.stage_table",
        ["id", "value"],
        query_label="load-stage",
    ) == (
        "/* analytics_toolkit query_label=load-stage */\n"
        'INSERT INTO schema.stage_table ("id", "value") VALUES %s'
    )

    assert load_sql_table_module.build_trino_batch_insert_sql(
        "schema.stage_table",
        ["id", "value"],
        row_count=2,
    ) == ('INSERT INTO schema.stage_table ("id", "value") VALUES (?, ?), (?, ?)')


def test_create_sql_table_only_generate_sql_uses_float64_for_decimal_clickhouse_columns() -> None:
    batch = pd.DataFrame(
        {
            "amount": [Decimal("1.20"), Decimal("2.50"), None],
            "label": ["ok", "still ok", None],
        }
    )

    sql = create_sql_table_module.create_sql_table(
        db_key="ch",
        table_name="schema.stage_table",
        df=batch,
        only_generate_sql=True,
    )

    assert "`amount` Nullable(Float64)" in sql
    assert "`label` Nullable(String)" in sql


def test_create_sql_table_only_generate_sql_uses_table_schema() -> None:
    sql = create_sql_table_module.create_sql_table(
        db_key="gp",
        table_name="schema.stage_table",
        table_schema={
            "amount": "NUMERIC(12, 2)",
            "created_at": "TIMESTAMP",
        },
        only_generate_sql=True,
    )

    assert '"amount" NUMERIC(12, 2)' in sql
    assert '"created_at" TIMESTAMP' in sql
    assert "appendonly=true" in sql
    assert "blocksize=32768" in sql
    assert "compresstype=zstd" in sql
    assert "compresslevel=4" in sql
    assert "orientation=column" in sql


def test_build_create_table_sqls_creates_clickhouse_distributed_pair() -> None:
    batch = pd.DataFrame(
        {
            "min_month_use": [date(2024, 1, 1)],
            "month_date": [date(2024, 2, 1)],
            "users": [10],
        }
    )

    sqls = create_sql_table_module._build_create_table_sqls(
        backend="ch",
        table_name=TEST_CH_TABLE,
        df=batch,
        ch_distributed_table=True,
        partition_by=["month_date"],
        order_by=["month_date", "min_month_use"],
        ch_sharding_key="cityHash64(month_date, min_month_use)",
    )

    assert len(sqls) == 4
    shard_sql, local_shard_sql, distributed_sql, local_distributed_sql = sqls
    assert "SETTINGS index_granularity" not in "\n".join(sqls)
    assert shard_sql.startswith(f"CREATE TABLE IF NOT EXISTS {TEST_CH_SHARD_TABLE}")
    assert "ON CLUSTER '{cluster}'" in shard_sql
    assert "ENGINE = ReplicatedMergeTree" in shard_sql
    assert "PARTITION BY `month_date`" in shard_sql
    assert "ORDER BY (`month_date`, `min_month_use`)" in shard_sql
    assert local_shard_sql.startswith(f"CREATE TABLE IF NOT EXISTS {TEST_CH_SHARD_TABLE}")
    assert "ON CLUSTER" not in local_shard_sql
    assert "UUID '" in local_shard_sql
    assert "ENGINE = ReplicatedMergeTree" in local_shard_sql
    assert distributed_sql.startswith(f"CREATE TABLE IF NOT EXISTS {TEST_CH_TABLE}")
    assert f"AS {TEST_CH_SHARD_TABLE}" not in distributed_sql
    assert "`min_month_use` Date" in distributed_sql
    assert "`month_date` Date" in distributed_sql
    assert "ENGINE = Distributed(" in distributed_sql
    assert "    '{cluster}'," in distributed_sql
    assert "    currentDatabase()," in distributed_sql
    assert f"    '{TEST_CH_SHARD_RELATION}'," in distributed_sql
    assert "    cityHash64(month_date, min_month_use)" in distributed_sql
    assert local_distributed_sql.startswith(f"CREATE TABLE IF NOT EXISTS {TEST_CH_TABLE}")
    assert "ON CLUSTER" not in local_distributed_sql
    assert "ENGINE = Distributed(" in local_distributed_sql


def test_build_create_table_sqls_clickhouse_only_shard_creates_local_target() -> None:
    batch = pd.DataFrame(
        {
            "min_month_use": [date(2024, 1, 1)],
            "month_date": [date(2024, 2, 1)],
            "users": [10],
        }
    )

    sqls = create_sql_table_module._build_create_table_sqls(
        backend="ch",
        table_name=TEST_CH_TABLE,
        df=batch,
        ch_distributed_table=True,
        ch_only_shard=True,
        partition_by=["month_date"],
        order_by=["month_date", "min_month_use"],
        ch_sharding_key="cityHash64(month_date, min_month_use)",
    )

    assert len(sqls) == 1
    create_sql = sqls[0]
    assert create_sql.startswith(f"CREATE TABLE IF NOT EXISTS {TEST_CH_TABLE}")
    assert TEST_CH_SHARD_TABLE not in create_sql
    assert "ON CLUSTER" not in create_sql
    assert "ENGINE = Distributed(" not in create_sql
    assert "ENGINE = ReplicatedMergeTree" in create_sql
    assert "PARTITION BY `month_date`" in create_sql
    assert "ORDER BY (`month_date`, `min_month_use`)" in create_sql


def test_wait_for_clickhouse_distributed_pair_polls_cluster_tables() -> None:
    class ClusterVisibilityClient:
        def __init__(self) -> None:
            self.queries: list[str] = []
            self.visible_counts = {
                "events": [1, 2, 3],
                "events_shard": [3],
            }

        def query(self, sql: str) -> object:
            self.queries.append(sql)
            if sql.startswith("SELECT getMacro("):
                return type("FakeResult", (), {"result_rows": [("core",)]})()
            if sql.startswith("EXISTS TABLE "):
                return type("FakeResult", (), {"result_rows": [(1,)]})()
            if "system, one" in sql:
                return type("FakeResult", (), {"result_rows": [(3,)]})()
            if "system, tables" in sql:
                table_name = sql.split("AND name = '", 1)[1].split("'", 1)[0]
                return type(
                    "FakeResult",
                    (),
                    {"result_rows": [(self.visible_counts[table_name].pop(0),)]},
                )()
            if "system, columns" in sql:
                return type(
                    "FakeResult",
                    (),
                    {"result_rows": [(sql.count("name = ") * 3 or 3,)]},
                )()
            raise AssertionError(f"Unexpected query: {sql}")

    client = ClusterVisibilityClient()

    ch_wait_module._wait_for_ch_distributed_table_pair(
        client,
        "analytics.events",
        ch_cluster="{cluster}",
        timeout_seconds=1,
        poll_interval_seconds=0,
    )

    assert "EXISTS TABLE analytics.events" in client.queries
    assert "EXISTS TABLE analytics.events_shard" in client.queries
    cluster_table_queries = [query for query in client.queries if "system, tables" in query]
    assert len(cluster_table_queries) == 4
    assert "clusterAllReplicas('core', system, tables)" in cluster_table_queries[0]
    assert "WHERE database = 'analytics'" in cluster_table_queries[0]
    assert "AND name = 'events'" in cluster_table_queries[0]
    assert "AND name = 'events_shard'" in cluster_table_queries[3]


def test_wait_for_clickhouse_distributed_pair_polls_cluster_schema() -> None:
    class ClusterSchemaClient:
        def __init__(self) -> None:
            self.queries: list[str] = []
            self.matching_counts = {
                "events": [1, 4],
                "events_shard": [4],
            }

        def query(self, sql: str) -> object:
            self.queries.append(sql)
            if sql.startswith("SELECT getMacro("):
                return type("FakeResult", (), {"result_rows": [("core",)]})()
            if sql.startswith("EXISTS TABLE "):
                return type("FakeResult", (), {"result_rows": [(1,)]})()
            if "system, one" in sql:
                return type("FakeResult", (), {"result_rows": [(1,)]})()
            if "FROM system.clusters" in sql:
                return type("FakeResult", (), {"result_rows": [(2,)]})()
            if "system, tables" in sql:
                return type("FakeResult", (), {"result_rows": [(2,)]})()
            if "system, columns" in sql:
                table_name = sql.split("AND table = '", 1)[1].split("'", 1)[0]
                return type(
                    "FakeResult",
                    (),
                    {"result_rows": [(self.matching_counts[table_name].pop(0),)]},
                )()
            raise AssertionError(f"Unexpected query: {sql}")

    client = ClusterSchemaClient()

    ch_wait_module._wait_for_ch_distributed_table_pair(
        client,
        "analytics.events",
        ch_cluster="{cluster}",
        timeout_seconds=1,
        poll_interval_seconds=0,
        expected_column_types={
            "month_date": "Date",
            "cheque_cnt_total": "Decimal(38, 5)",
        },
    )

    cluster_column_queries = [query for query in client.queries if "system, columns" in query]
    assert len(cluster_column_queries) == 3
    assert "clusterAllReplicas('core', system, columns)" in cluster_column_queries[0]
    assert "WHERE database = 'analytics'" in cluster_column_queries[0]
    assert "AND table = 'events'" in cluster_column_queries[0]
    assert (
        "name = 'month_date' AND replaceRegexpAll(type, '\\\\s+', '') = 'Date'"
        in cluster_column_queries[0]
    )
    assert (
        "name = 'cheque_cnt_total' "
        "AND replaceRegexpAll(type, '\\\\s+', '') = 'Decimal(38,5)'" in cluster_column_queries[0]
    )
    assert "AND table = 'events_shard'" in cluster_column_queries[2]


def test_clickhouse_post_create_wait_uses_independent_policy_clusters() -> None:
    class SplitClusterClient:
        def __init__(self) -> None:
            self.queries: list[str] = []

        def query(self, sql: str) -> object:
            self.queries.append(sql)
            if sql.startswith("SELECT getMacro("):
                rows = [("routing",)]
            elif sql.startswith("EXISTS TABLE "):
                rows = [(1,)]
            elif "FROM system.clusters" in sql:
                cluster = sql.split("cluster = '", 1)[1].split("'", 1)[0]
                rows = [(22 if cluster == "routing" else 2,)]
            elif "system, one" in sql or "system, tables" in sql or "system, columns" in sql:
                rows = [(22 if "'routing'" in sql else 2,)]
            else:
                message = f"Unexpected query: {sql}"
                raise AssertionError(message)
            return SimpleNamespace(result_rows=rows)

    client = SplitClusterClient()
    policy = SimpleNamespace(
        shard_on_cluster="core",
        distributed_on_cluster="{cluster}",
        distributed_cluster="core",
        ddl_ready_timeout_seconds=17.0,
    )

    ch_wait_module.after_create_table(
        object(),
        client,
        "analytics.events",
        ch_distributed_table=True,
        expected_column_types={"event_id": "Int64"},
        ch_creation_policy=policy,
    )

    cluster_table_queries = [query for query in client.queries if "system, tables" in query]
    cluster_schema_queries = [query for query in client.queries if "system, columns" in query]
    assert any(
        "clusterAllReplicas('routing'" in query and "events'" in query
        for query in cluster_table_queries
    )
    assert any(
        "clusterAllReplicas('core'" in query and "events_shard'" in query
        for query in cluster_table_queries
    )
    assert not any(
        "clusterAllReplicas('routing'" in query and "events_shard'" in query
        for query in cluster_table_queries
    )
    assert any(
        "clusterAllReplicas('routing'" in query and "events'" in query
        for query in cluster_schema_queries
    )
    assert any(
        "clusterAllReplicas('core'" in query and "events_shard'" in query
        for query in cluster_schema_queries
    )


def test_clickhouse_post_create_wait_shares_one_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monotonic_values = iter([100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 106.0])
    observed_timeouts: list[float] = []
    monkeypatch.setattr(ch_wait_module.time, "monotonic", lambda: next(monotonic_values))
    monkeypatch.setattr(
        ch_wait_module,
        "_wait_for_ch_table",
        lambda *_args, timeout_seconds, **_kwargs: observed_timeouts.append(timeout_seconds),
    )
    monkeypatch.setattr(
        ch_wait_module,
        "_wait_for_ch_table_on_cluster",
        lambda *_args, timeout_seconds, **_kwargs: observed_timeouts.append(timeout_seconds),
    )
    monkeypatch.setattr(
        ch_wait_module,
        "_wait_for_ch_table_schema_on_cluster",
        lambda *_args, timeout_seconds, **_kwargs: observed_timeouts.append(timeout_seconds),
    )

    ch_wait_module._wait_for_ch_distributed_table_pair(
        object(),
        "analytics.events",
        timeout_seconds=10,
        expected_column_types={"event_id": "Int64"},
        shard_on_cluster="core",
        distributed_on_cluster="routing",
        routing_cluster=None,
    )

    assert observed_timeouts == [9.0, 8.0, 7.0, 6.0, 5.0, 4.0]


def test_clickhouse_post_create_rejects_uncovered_routing_cluster() -> None:
    class MismatchedRoutingClient:
        def query(self, sql: str) -> object:
            if sql.startswith("SELECT getMacro("):
                rows = [("routing",)]
            elif sql.startswith("EXISTS TABLE "):
                rows = [(1,)]
            elif "SELECT DISTINCT host_name" in sql:
                rows = [(f"host-{index:02d}",) for index in range(22)]
            elif "FROM system.clusters" in sql or "system, one" in sql:
                rows = [(22 if "'routing'" in sql else 2,)]
            elif "system, tables" in sql and sql.startswith("SELECT hostName()"):
                rows = [
                    ("host-00", "analytics", "events_shard", "ReplicatedMergeTree"),
                    ("host-01", "analytics", "events_shard", "ReplicatedMergeTree"),
                ]
            elif "system, tables" in sql:
                is_shard = "events_shard" in sql
                count = 2 if is_shard and "'routing'" in sql else 22
                if is_shard and "'core'" in sql:
                    count = 2
                rows = [(count,)]
            elif "system, columns" in sql:
                rows = [(2,)]
            else:
                message = f"Unexpected query: {sql}"
                raise AssertionError(message)
            return SimpleNamespace(result_rows=rows)

    policy = SimpleNamespace(
        shard_on_cluster="core",
        distributed_on_cluster="{cluster}",
        distributed_cluster="{cluster}",
        ddl_ready_timeout_seconds=17.0,
    )

    with pytest.raises(
        ch_wait_module.SqlConfigError,
        match=r"routing cluster 'routing'.*2/22.*shard DDL uses cluster 'core'",
    ) as exc_info:
        ch_wait_module.after_create_table(
            object(),
            MismatchedRoutingClient(),
            "analytics.events",
            ch_distributed_table=True,
            expected_column_types={"event_id": "Int64"},
            ch_creation_policy=policy,
        )

    assert "host-02" in str(exc_info.value)
    assert "ch_distributed_cluster" in str(exc_info.value)


def test_clickhouse_post_create_rejects_routing_schema_mismatch() -> None:
    class RoutingSchemaClient:
        def query(self, sql: str) -> object:
            if (
                "system, one" in sql
                or "FROM system.clusters" in sql
                or "system, tables" in sql
            ):
                rows = [(2,)]
            elif "GROUP BY name, type" in sql:
                rows = [("event_id", "UInt8", 2)]
            elif "system, columns" in sql:
                rows = [(0,)]
            else:
                message = f"Unexpected query: {sql}"
                raise AssertionError(message)
            return SimpleNamespace(result_rows=rows)

    with pytest.raises(
        ch_wait_module.SqlConfigError,
        match=r"schema is not ready.*observed 0/2",
    ) as exc_info:
        ch_wait_module._validate_ch_shard_routing_cluster(
            RoutingSchemaClient(),
            "analytics.events_shard",
            ch_cluster="core",
            shard_on_cluster="core",
            expected_column_types={"event_id": "Int64"},
        )

    assert "observed UInt8 on 2 host" in str(exc_info.value)


def test_wait_for_clickhouse_distributed_pair_absence_polls_cluster_tables() -> None:
    class ClusterDropClient:
        def __init__(self) -> None:
            self.queries: list[str] = []
            self.visible_rows = [
                [
                    ("host-a", "analytics", "events", "Distributed"),
                    ("host-a", "analytics", "events_shard", "ReplicatedMergeTree"),
                ],
                [],
            ]

        def query(self, sql: str) -> object:
            self.queries.append(sql)
            if sql.startswith("SELECT getMacro("):
                return type("FakeResult", (), {"result_rows": [("core",)]})()
            if "system, one" in sql:
                return type("FakeResult", (), {"result_rows": [(2,)]})()
            if "FROM system.clusters" in sql:
                return type("FakeResult", (), {"result_rows": [(2,)]})()
            if "system, tables" in sql:
                return type(
                    "FakeResult",
                    (),
                    {"result_rows": self.visible_rows.pop(0)},
                )()
            raise AssertionError(f"Unexpected query: {sql}")

    client = ClusterDropClient()

    ch_wait_module._wait_for_ch_distributed_table_pair_absence(
        client,
        "analytics.events",
        ch_cluster="{cluster}",
        timeout_seconds=1,
        poll_interval_seconds=0,
    )

    cluster_table_queries = [query for query in client.queries if "system, tables" in query]
    assert len(cluster_table_queries) == 2
    assert "AND name = 'events'" in cluster_table_queries[0]
    assert "AND name = 'events_shard'" in cluster_table_queries[0]


def test_wait_for_clickhouse_distributed_pair_absence_reports_leftover_hosts() -> None:
    class StaleDropClient:
        def query(self, sql: str) -> object:
            if sql.startswith("SELECT getMacro("):
                return type("FakeResult", (), {"result_rows": [("core",)]})()
            if "system, one" in sql:
                return type("FakeResult", (), {"result_rows": [(2,)]})()
            if "FROM system.clusters" in sql:
                return type("FakeResult", (), {"result_rows": [(2,)]})()
            if "system, tables" in sql:
                return type(
                    "FakeResult",
                    (),
                    {
                        "result_rows": [
                            (
                                "host-b",
                                "analytics",
                                "events_shard",
                                "ReplicatedMergeTree",
                            )
                        ]
                    },
                )()
            raise AssertionError(f"Unexpected query: {sql}")

    with pytest.raises(TimeoutError) as exc_info:
        ch_wait_module._wait_for_ch_distributed_table_pair_absence(
            StaleDropClient(),
            "analytics.events",
            ch_cluster="{cluster}",
            timeout_seconds=0,
            poll_interval_seconds=0,
        )

    message = str(exc_info.value)
    assert "host-b: analytics.events_shard (ReplicatedMergeTree)" in message
    assert "ch_retry_per_host_drops=True" in message


def test_wait_for_clickhouse_distributed_pair_reports_schema_mismatch() -> None:
    class StaleSchemaClient:
        def query(self, sql: str) -> object:
            if sql.startswith("SELECT getMacro("):
                return type("FakeResult", (), {"result_rows": [("core",)]})()
            if sql.startswith("EXISTS TABLE "):
                return type("FakeResult", (), {"result_rows": [(1,)]})()
            if "system, one" in sql:
                return type("FakeResult", (), {"result_rows": [(2,)]})()
            if "system, tables" in sql:
                return type("FakeResult", (), {"result_rows": [(2,)]})()
            if "GROUP BY name, type" in sql:
                return type(
                    "FakeResult",
                    (),
                    {
                        "result_rows": [
                            ("month_date", "Date", 2),
                            ("cheque_cnt_total", "UInt8", 2),
                        ]
                    },
                )()
            if "system, columns" in sql:
                return type("FakeResult", (), {"result_rows": [(2,)]})()
            raise AssertionError(f"Unexpected query: {sql}")

    with pytest.raises(TimeoutError) as exc_info:
        ch_wait_module._wait_for_ch_distributed_table_pair(
            StaleSchemaClient(),
            "analytics.events",
            ch_cluster="{cluster}",
            timeout_seconds=0,
            poll_interval_seconds=0,
            expected_column_types={
                "month_date": "Date",
                "cheque_cnt_total": "Decimal(38, 5)",
            },
        )

    message = str(exc_info.value)
    assert "schema did not match expected columns" in message
    assert "cheque_cnt_total" in message
    assert "expected Decimal(38, 5)" in message
    assert "observed UInt8 on 2 host(s)" in message


def test_load_df_clickhouse_creates_pair_and_loads_distributed_table(monkeypatch) -> None:
    client = FakeClickHouseClient()
    batch = pd.DataFrame(
        {
            "month_date": [date(2024, 2, 1)],
            "min_month_use": [date(2024, 1, 1)],
            "users": [10],
        }
    )

    monkeypatch.setattr(
        load_df_module,
        "get_sql_connection",
        lambda connection_type: client,
    )
    monkeypatch.setattr(load_df_module, "table_exists", lambda *args, **kwargs: False)

    inserted_rows = load_df_module.load_df(
        "ch",
        TEST_CH_TABLE,
        batch,
        retry_cnt=1,
        timeout_increment=0,
        partition_by=["month_date"],
        order_by=["month_date", "min_month_use"],
        ch_sharding_key="cityHash64(month_date, min_month_use)",
    )

    assert inserted_rows == 1
    assert f"DROP TABLE IF EXISTS {TEST_CH_TABLE}" in client.commands
    assert f"DROP TABLE IF EXISTS {TEST_CH_SHARD_TABLE}" in client.commands
    assert f"DROP TABLE IF EXISTS {TEST_CH_TABLE} ON CLUSTER '{{cluster}}'" in client.commands
    assert f"DROP TABLE IF EXISTS {TEST_CH_SHARD_TABLE} ON CLUSTER '{{cluster}}'" in client.commands
    assert "SETTINGS index_granularity" not in "\n".join(client.commands)
    assert any(
        command.startswith(f"CREATE TABLE IF NOT EXISTS {TEST_CH_SHARD_TABLE}")
        for command in client.commands
    )
    assert any(
        command.startswith(f"CREATE TABLE IF NOT EXISTS {TEST_CH_TABLE}")
        and "ON CLUSTER '{cluster}'" in command
        for command in client.commands
    )
    assert any(
        command.startswith(f"CREATE TABLE IF NOT EXISTS {TEST_CH_TABLE}")
        and "ON CLUSTER" not in command
        for command in client.commands
    )
    assert client.calls[0]["table"] == TEST_CH_TABLE
    assert client.close_calls == 4


def test_finalize_stage_table_clickhouse_recreates_pair_and_inserts_target() -> None:
    client = FakeClickHouseClient()
    batch = pd.DataFrame(
        {
            "month_date": [date(2024, 2, 1)],
            "min_month_use": [date(2024, 1, 1)],
            "users": [10],
        }
    )

    table_ops_module.finalize_stage_table(
        connection_type="ch",
        connection=client,
        stage_table=TEST_CH_STAGE_TABLE,
        target_table=TEST_CH_TABLE,
        replace_target_table=True,
        target_exists=True,
        sample_batch=batch,
        partition_by=["month_date"],
        order_by=["month_date", "min_month_use"],
        ch_sharding_key="cityHash64(month_date, min_month_use)",
    )

    assert f"DROP TABLE IF EXISTS {TEST_CH_TABLE}" in client.commands
    assert f"DROP TABLE IF EXISTS {TEST_CH_SHARD_TABLE}" in client.commands
    assert f"DROP TABLE IF EXISTS {TEST_CH_TABLE} ON CLUSTER '{{cluster}}'" in client.commands
    assert f"DROP TABLE IF EXISTS {TEST_CH_SHARD_TABLE} ON CLUSTER '{{cluster}}'" in client.commands
    assert any(
        command.startswith(f"CREATE TABLE IF NOT EXISTS {TEST_CH_SHARD_TABLE}")
        for command in client.commands
    )
    assert any(
        command.startswith(f"CREATE OR REPLACE TABLE {TEST_CH_TABLE}")
        and "ON CLUSTER '{cluster}'" in command
        for command in client.commands
    )
    assert any(
        command.startswith(f"CREATE TABLE IF NOT EXISTS {TEST_CH_TABLE}")
        and "ON CLUSTER" not in command
        for command in client.commands
    )
    assert client.commands[-1] == (
        f"INSERT INTO {TEST_CH_TABLE} SELECT * FROM {TEST_CH_STAGE_TABLE}"
    )
    assert not any(query.startswith("DESCRIBE TABLE ") for query in client.queries)


def test_finalize_stage_table_clickhouse_ensures_existing_pair_before_insert() -> None:
    client = FakeClickHouseClient()
    batch = pd.DataFrame(
        {
            "month_date": [date(2024, 2, 1)],
            "users": [10],
        }
    )
    column_types = {
        "month_date": "Nullable(Date)",
        "users": "Nullable(Int64)",
    }

    table_ops_module.finalize_stage_table(
        connection_type="ch",
        connection=client,
        stage_table=TEST_CH_STAGE_TABLE,
        target_table=TEST_CH_TABLE,
        replace_target_table=False,
        target_exists=True,
        sample_batch=batch,
        insert_column_types=column_types,
        partition_by=["month_date"],
        order_by=["month_date"],
        ch_cluster="core",
    )

    assert f"DROP TABLE IF EXISTS {TEST_CH_TABLE}" not in client.commands
    assert any(
        command.startswith(f"CREATE TABLE IF NOT EXISTS {TEST_CH_SHARD_TABLE}")
        and "ON CLUSTER core" in command
        for command in client.commands
    )
    assert any(
        command.startswith(f"CREATE TABLE IF NOT EXISTS {TEST_CH_TABLE}")
        and "ON CLUSTER core" in command
        for command in client.commands
    )
    assert client.commands[-1] == (
        f"INSERT INTO {TEST_CH_TABLE} (`month_date`, `users`) "
        f"SELECT CAST(`month_date` AS Nullable(Date)) AS `month_date`, "
        f"CAST(`users` AS Nullable(Int64)) AS `users` "
        f"FROM {TEST_CH_STAGE_TABLE}"
    )

    insert_index = client.commands.index(client.commands[-1])
    create_indices = [
        idx
        for idx, command in enumerate(client.commands)
        if command.startswith("CREATE TABLE IF NOT EXISTS")
    ]
    assert create_indices
    assert max(create_indices) < insert_index


def test_finalize_stage_table_clickhouse_uses_explicit_types_and_casts_insert() -> None:
    client = FakeClickHouseClient()
    batch = pd.DataFrame(
        {
            "month_date": ["2024-02-01"],
            "users": ["10"],
        }
    )
    column_types = {
        "month_date": "Nullable(Date)",
        "users": "Nullable(Int64)",
    }

    table_ops_module.finalize_stage_table(
        connection_type="ch",
        connection=client,
        stage_table=TEST_CH_STAGE_TABLE,
        target_table=TEST_CH_TABLE,
        replace_target_table=True,
        target_exists=True,
        sample_batch=batch,
        target_column_types=column_types,
        insert_column_types=column_types,
        partition_by=["month_date"],
        order_by=["month_date"],
    )

    create_sql = "\n".join(client.commands)
    assert "`month_date` Nullable(Date)" in create_sql
    assert "`users` Nullable(Int64)" in create_sql
    assert client.commands[-1] == (
        f"INSERT INTO {TEST_CH_TABLE} (`month_date`, `users`) "
        f"SELECT CAST(`month_date` AS Nullable(Date)) AS `month_date`, "
        f"CAST(`users` AS Nullable(Int64)) AS `users` "
        f"FROM {TEST_CH_STAGE_TABLE}"
    )


def test_finalize_stage_table_trino_replace_recreates_target() -> None:
    connection = FakeDbapiConnection()
    batch = pd.DataFrame(
        {
            "contact_id": ["1"],
            "first_game_dt": [date(2026, 1, 1)],
            "last_game_dt": [date(2026, 1, 2)],
        }
    )
    column_types = {
        "contact_id": "VARCHAR",
        "first_game_dt": "DATE",
        "last_game_dt": "DATE",
    }

    table_ops_module.finalize_stage_table(
        connection_type="trino",
        connection=connection,
        stage_table="iceberg.pa_core_stage.target__stage",
        target_table="iceberg.pa_core_sandbox.target",
        replace_target_table=True,
        target_exists=True,
        sample_batch=batch,
        target_column_types=column_types,
        insert_column_types=column_types,
    )

    assert connection.executed[0] == "DROP TABLE IF EXISTS iceberg.pa_core_sandbox.target"
    assert connection.executed[1].startswith(
        'CREATE TABLE iceberg.pa_core_sandbox.target ("contact_id" VARCHAR, '
        '"first_game_dt" DATE, "last_game_dt" DATE)'
    )
    assert connection.executed[2] == (
        'INSERT INTO iceberg.pa_core_sandbox.target ("contact_id", '
        '"first_game_dt", "last_game_dt") '
        'SELECT CAST("contact_id" AS VARCHAR) AS "contact_id", '
        'CAST("first_game_dt" AS DATE) AS "first_game_dt", '
        'CAST("last_game_dt" AS DATE) AS "last_game_dt" '
        "FROM iceberg.pa_core_stage.target__stage"
    )
    assert not any(
        sql.startswith("DELETE FROM iceberg.pa_core_sandbox.target") for sql in connection.executed
    )


def test_finalize_stage_table_greenplum_upsert_deletes_then_inserts() -> None:
    connection = FakeDbapiConnection()
    batch = pd.DataFrame({"id": [1], "sub_id": [None], "score": [10]})

    table_ops_module.finalize_stage_table(
        connection_type="gp",
        connection=connection,
        stage_table="sandbox.target__stage",
        target_table="sandbox.target",
        replace_target_table=True,
        target_exists=True,
        sample_batch=batch,
        write_mode="upsert",
        key_columns=["id", "sub_id"],
        insert_column_types={"id": "BIGINT", "sub_id": "BIGINT", "score": "INTEGER"},
    )

    assert connection.executed[0] == (
        "DELETE FROM sandbox.target AS target_dst\n"
        "USING sandbox.target__stage AS stage_src\n"
        'WHERE (target_dst."id" = stage_src."id" '
        'OR (target_dst."id" IS NULL AND stage_src."id" IS NULL)) '
        'AND (target_dst."sub_id" = stage_src."sub_id" '
        'OR (target_dst."sub_id" IS NULL AND stage_src."sub_id" IS NULL))'
    )
    assert connection.executed[1].startswith(
        'INSERT INTO sandbox.target ("id", "sub_id", "score") '
    )


def test_finalize_stage_table_trino_upsert_replaces_affected_partitions() -> None:
    connection = FakeDbapiConnection(rows=[("2026-06-24",)])
    batch = pd.DataFrame({"id": [1], "score": [10]})

    table_ops_module.finalize_stage_table(
        connection_type="trino",
        connection=connection,
        stage_table="sandbox.target__stage",
        target_table="sandbox.target",
        replace_target_table=True,
        target_exists=True,
        sample_batch=batch,
        write_mode="upsert",
        key_columns=["id"],
        upsert_partition_column="event_date",
        final_upsert_stage_table="sandbox.target__final_stage",
        trino_upsert_partition_drop_sql_template=(
            "ALTER TABLE {table} DROP PARTITION ({partition_column} = {partition_value})"
        ),
        insert_column_types={"id": "BIGINT", "score": "INTEGER"},
    )

    assert connection.executed[0] == ('SELECT DISTINCT "event_date" FROM sandbox.target__stage')
    assert connection.executed[1].startswith(
        'INSERT INTO sandbox.target__final_stage ("id", "score")\n'
        'SELECT target_dst."id", target_dst."score"\n'
        "FROM sandbox.target AS target_dst"
    )
    assert connection.executed[3] == (
        "ALTER TABLE sandbox.target DROP PARTITION (\"event_date\" = '2026-06-24')"
    )
    assert connection.executed[4].startswith('INSERT INTO sandbox.target ("id", "score") ')


def test_finalize_stage_table_clickhouse_upsert_drops_shard_partitions() -> None:
    client = FakeClickHouseClient()
    batch = pd.DataFrame({"id": [1], "score": [10]})

    table_ops_module.finalize_stage_table(
        connection_type="ch",
        connection=client,
        stage_table=TEST_CH_STAGE_TABLE,
        target_table=TEST_CH_TABLE,
        replace_target_table=True,
        target_exists=True,
        sample_batch=batch,
        write_mode="upsert",
        key_columns=["id"],
        upsert_partition_column="id",
        final_upsert_stage_table=f"{TEST_CH_TABLE}__final_stage",
        insert_column_types={"id": "UInt64", "score": "Int64"},
        ch_cluster="core",
    )

    drop_sql = next(sql for sql in client.commands if "DROP PARTITION" in sql)
    assert drop_sql == f"ALTER TABLE {TEST_CH_SHARD_TABLE} ON CLUSTER core DROP PARTITION 1"
    assert client.commands[-1].startswith(f"INSERT INTO {TEST_CH_TABLE} (`id`, `score`) ")


def test_finalize_stage_table_clickhouse_only_shard_upsert_deletes_target() -> None:
    client = FakeClickHouseClient()
    batch = pd.DataFrame({"id": [1], "score": [10]})

    table_ops_module.finalize_stage_table(
        connection_type="ch",
        connection=client,
        stage_table=TEST_CH_STAGE_TABLE,
        target_table=TEST_CH_TABLE,
        replace_target_table=True,
        target_exists=True,
        sample_batch=batch,
        write_mode="upsert",
        key_columns=["id"],
        upsert_partition_column="id",
        final_upsert_stage_table=f"{TEST_CH_TABLE}__final_stage",
        insert_column_types={"id": "UInt64", "score": "Int64"},
        ch_cluster="core",
        ch_only_shard=True,
    )

    drop_sql = next(sql for sql in client.commands if "DROP PARTITION" in sql)
    assert drop_sql == f"ALTER TABLE {TEST_CH_TABLE} DROP PARTITION 1"
    assert "ON CLUSTER" not in drop_sql


def test_finalize_stage_table_upsert_missing_target_creates_and_inserts() -> None:
    connection = FakeDbapiConnection()
    batch = pd.DataFrame({"id": [1], "score": [10]})

    table_ops_module.finalize_stage_table(
        connection_type="gp",
        connection=connection,
        stage_table="sandbox.target__stage",
        target_table="sandbox.target",
        replace_target_table=True,
        target_exists=False,
        sample_batch=batch,
        write_mode="upsert",
        key_columns=["id"],
        insert_column_types={"id": "BIGINT", "score": "INTEGER"},
        target_column_types={"id": "BIGINT", "score": "INTEGER"},
    )

    assert connection.executed[0].startswith("CREATE TABLE sandbox.target")
    assert connection.executed[1].startswith('INSERT INTO sandbox.target ("id", "score") ')


def test_load_df_trino_parquet_stage_routes_through_external_table(
    monkeypatch: pytest.MonkeyPatch,
    write_sql_connections: Any,
) -> None:
    _write_trino_connections(
        write_sql_connections,
        s3_transfer_staging_location="s3://bucket/tmp/analytics_toolkit_transfer",
        s3_transfer_staging_schema="hive.pa_core_stage",
    )
    connection = FakeDbapiConnection()
    writes: list[dict[str, object]] = []
    inserts: list[tuple[str, str]] = []
    cleaned_locations: list[str] = []

    monkeypatch.setattr(load_df_module, "get_sql_connection", lambda key: connection)
    monkeypatch.setattr(
        load_df_module,
        "table_exists",
        lambda connection_type, connection, table_name, **kwargs: (
            table_name == "iceberg.sandbox.target"
        ),
    )
    monkeypatch.setattr(
        load_df_module,
        "_create_sql_table_with_connection",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        load_df_module,
        "get_table_column_types",
        lambda *args, **kwargs: {"id": "BIGINT", "label": "VARCHAR"},
    )
    monkeypatch.setattr(
        load_df_module,
        "ensure_parquet_staging_dependencies",
        lambda: ("pa", "pq", "fsspec"),
    )
    monkeypatch.setattr(
        load_df_module,
        "write_dataframe_to_parquet_stage",
        lambda df, **kwargs: (
            writes.append(
                {
                    "rows": len(df),
                    "location": kwargs["stage_external_location"],
                    "row_group_size": kwargs["row_group_size"],
                    "pa": kwargs["pa"],
                    "pq": kwargs["pq"],
                    "fsspec": kwargs["fsspec_module"],
                }
            )
            or len(df)
        ),
    )
    monkeypatch.setattr(
        load_df_module,
        "insert_from_table",
        lambda connection_type, connection, target, stage, **kwargs: inserts.append(
            (target, stage)
        ),
    )
    monkeypatch.setattr(
        load_df_module,
        "insert_table_batch",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("Parquet load_df must not use row inserts")
        ),
    )
    monkeypatch.setattr(load_df_module, "analyze_table", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        load_df_module,
        "cleanup_parquet_stage_location",
        lambda location: cleaned_locations.append(location),
    )

    rows = load_df_module.load_df(
        "trino_stage",
        "iceberg.sandbox.target",
        pd.DataFrame({"id": [1, 2], "label": ["a", "b"]}),
        append=True,
        retry_cnt=1,
        timeout_increment=0,
    )

    assert rows == 2
    assert writes == [
        {
            "rows": 2,
            "location": cleaned_locations[0],
            "row_group_size": 50_000,
            "pa": "pa",
            "pq": "pq",
            "fsspec": "fsspec",
        }
    ]
    assert inserts[0][0] == "iceberg.sandbox.target"
    assert inserts[0][1].startswith("hive.pa_core_stage.daf6958bfec1c9f7__")
    assert cleaned_locations[0].startswith("s3://bucket/tmp/analytics_toolkit_transfer/target/")
    assert "__analytics_toolkit_target_user__stage__" in cleaned_locations[0]
    assert any(
        sql.startswith("CREATE TABLE hive.pa_core_stage.daf6958bfec1c9f7__")
        and "WITH (format = 'PARQUET', external_location = 's3://bucket/tmp/" in sql
        for sql in connection.executed
    )
    assert any(
        sql.startswith("DROP TABLE IF EXISTS hive.pa_core_stage.daf6958bfec1c9f7__")
        for sql in connection.executed
    )


def test_load_df_trino_without_staging_location_keeps_insert_path(
    monkeypatch: pytest.MonkeyPatch,
    write_sql_connections: Any,
) -> None:
    _write_trino_connections(write_sql_connections, s3_transfer_staging_location=None)
    connection = FakeDbapiConnection()
    inserted_tables: list[str] = []

    monkeypatch.setattr(load_df_module, "get_sql_connection", lambda key: connection)
    monkeypatch.setattr(load_df_module, "table_exists", lambda *args, **kwargs: False)
    monkeypatch.setattr(
        load_df_module,
        "_create_sql_table_with_connection",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(load_df_module, "get_table_column_types", lambda *args, **kwargs: {})
    monkeypatch.setattr(
        load_df_module,
        "insert_table_batch",
        lambda connection_type, connection_ref, table_name, batch, **kwargs: (
            inserted_tables.append(table_name) or len(batch)
        ),
    )
    monkeypatch.setattr(load_df_module, "analyze_table", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        load_df_module,
        "ensure_parquet_staging_dependencies",
        lambda: (_ for _ in ()).throw(AssertionError("Parquet dependencies should not be loaded")),
    )

    rows = load_df_module.load_df(
        "trino_stage",
        "iceberg.sandbox.target",
        pd.DataFrame({"id": [1]}),
        retry_cnt=1,
        timeout_increment=0,
    )

    assert rows == 1
    assert inserted_tables == ["iceberg.sandbox.target"]


def test_load_df_trino_parquet_upsert_uses_merge_from_stage(
    monkeypatch: pytest.MonkeyPatch,
    write_sql_connections: Any,
) -> None:
    _write_trino_connections(
        write_sql_connections,
        s3_transfer_staging_location="s3://bucket/tmp/analytics_toolkit_transfer",
    )
    connection = FakeDbapiConnection()
    uniqueness_checks: list[tuple[str, list[str]]] = []
    upserts: list[dict[str, object]] = []

    monkeypatch.setattr(load_df_module, "get_sql_connection", lambda key: connection)
    monkeypatch.setattr(
        load_df_module,
        "table_exists",
        lambda connection_type, connection, table_name, **kwargs: (
            table_name == "iceberg.sandbox.target"
        ),
    )
    monkeypatch.setattr(
        load_df_module,
        "get_table_column_types",
        lambda *args, **kwargs: {"id": "BIGINT", "score": "INTEGER"},
    )
    monkeypatch.setattr(
        load_df_module,
        "ensure_parquet_staging_dependencies",
        lambda: ("pa", "pq", "fsspec"),
    )
    monkeypatch.setattr(
        load_df_module,
        "write_dataframe_to_parquet_stage",
        lambda df, **kwargs: len(df),
    )
    monkeypatch.setattr(
        load_df_module,
        "validate_stage_uniqueness",
        lambda connection_type, connection, stage_table, key_columns: uniqueness_checks.append(
            (stage_table, list(key_columns))
        ),
    )
    monkeypatch.setattr(
        load_df_module,
        "upsert_stage_table",
        lambda connection_type, connection, target, stage, columns, key_columns, **kwargs: (
            upserts.append(
                {
                    "target": target,
                    "stage": stage,
                    "columns": list(columns),
                    "key_columns": list(key_columns),
                }
            )
        ),
    )
    monkeypatch.setattr(load_df_module, "analyze_table", lambda *args, **kwargs: None)
    monkeypatch.setattr(load_df_module, "cleanup_parquet_stage_location", lambda *args: None)

    rows = load_df_module.load_df(
        "trino_stage",
        "iceberg.sandbox.target",
        pd.DataFrame({"id": [1, 2], "score": [10, 20]}),
        write_mode="upsert",
        key_columns=["id"],
        upsert_partition_column="id",
        retry_cnt=1,
        timeout_increment=0,
    )

    assert rows == 2
    assert uniqueness_checks[0][1] == ["id"]
    assert upserts == [
        {
            "target": "iceberg.sandbox.target",
            "stage": uniqueness_checks[0][0],
            "columns": ["id", "score"],
            "key_columns": ["id"],
        }
    ]


def test_load_df_trino_parquet_dry_run_includes_stage_location(
    write_sql_connections: Any,
) -> None:
    _write_trino_connections(
        write_sql_connections,
        s3_transfer_staging_location="s3://bucket/tmp/analytics_toolkit_transfer",
    )

    plan = load_df_module.load_df(
        "trino_stage",
        "iceberg.sandbox.target",
        pd.DataFrame({"id": [1], "label": ["a"]}),
        table_schema={"id": "BIGINT", "label": "VARCHAR"},
        dry_run=True,
    )

    assert plan.options["use_parquet_staging"] is True
    assert plan.metadata.stage_table == (
        "hive.pa_core_stage.daf6958bfec1c9f7__targetdryrun"
    )
    assert plan.metadata.stage_external_location == (
        "s3://bucket/tmp/analytics_toolkit_transfer/target/"
        "__analytics_toolkit_target_user__stage__dryrun/"
    )
    assert [statement.phase for statement in plan.statements] == [
        "drop_target",
        "create_target",
        "create_stage",
        "load_stage",
        "insert_from_stage",
        "drop_stage",
        "cleanup_stage_location",
        "analyze",
        "count_target",
    ]
    assert any(
            "CREATE TABLE hive.pa_core_stage.daf6958bfec1c9f7__targetdryrun "
        in sql
        and "external_location = 's3://bucket/tmp/analytics_toolkit_transfer/target/" in sql
        for sql in plan.sqls
    )
    assert any(
        sql.startswith("WRITE PARQUET FILES TO s3://bucket/tmp/analytics_toolkit_transfer/target/")
        for sql in plan.sqls
    )
    assert any(sql.startswith("DELETE STAGE FILES s3://bucket/tmp/") for sql in plan.sqls)


def test_load_df_parquet_stage_does_not_inherit_sql_staging_properties(
    write_sql_connections: Any,
) -> None:
    _write_trino_connections(
        write_sql_connections,
        s3_transfer_staging_location="s3://bucket/tmp/analytics_toolkit_transfer",
        ddl_defaults={
            "regular": {},
            "staging": {"compression_codec": "'ZSTD'"},
            "parquet_staging": {"parquet_marker": 7},
        },
    )

    plan = load_df_module.load_df(
        "trino_stage",
        "iceberg.sandbox.target",
        pd.DataFrame({"id": [1]}),
        table_schema={"id": "BIGINT"},
        dry_run=True,
    )

    create_stage_sql = next(
        statement.sql for statement in plan.statements if statement.phase == "create_stage"
    )
    assert "compression_codec" not in create_stage_sql
    assert "parquet_marker = 7" in create_stage_sql


def test_load_df_parquet_runtime_passes_only_parquet_properties(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    options = load_df_module.LoadOptions(
        connection_key="trino",
        connection_backend="trino",
        destination_table="iceberg.sandbox.target",
        table_schema={"id": "BIGINT"},
        s3_transfer_staging_schema="hive.stage",
        s3_transfer_staging_location="s3://bucket/stage",
        staging_ddl_properties={"compression_codec": "'ZSTD'"},
        parquet_ddl_properties={"parquet_marker": 7},
    )
    state = load_df_module.LoadState(target_exists=True, original_target_exists=True)
    monkeypatch.setattr(load_df_module, "table_exists", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        load_df_module,
        "build_stage_table_name",
        lambda *_args, **_kwargs: "hive.stage.shared",
    )
    monkeypatch.setattr(
        load_df_module,
        "build_stage_external_location",
        lambda *_args, **_kwargs: "s3://bucket/stage/shared/",
    )

    def build_sql(*_args: object, **kwargs: object) -> str:
        captured.update(kwargs)
        return "CREATE TABLE hive.stage.shared"

    monkeypatch.setattr(load_df_module, "build_create_parquet_stage_table_sql", build_sql)
    monkeypatch.setattr(
        load_df_module,
        "get_backend_adapter",
        lambda _backend: SimpleNamespace(execute_command=lambda *_args: None),
    )

    load_df_module._create_load_parquet_stage_table(options, state, object())

    assert captured["ddl_properties"] == {"parquet_marker": 7}


def test_write_dataframe_to_parquet_stage_uses_one_spooled_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active_spooled_files = 0
    max_active_spooled_files = 0
    uploaded: list[tuple[str, object]] = []

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

        def seek(self, position: int) -> None:
            assert position == 0

        def close(self) -> None:
            nonlocal active_spooled_files
            self.closed = True
            active_spooled_files -= 1

        def getvalue(self) -> bytes:
            raise AssertionError("load_df Parquet staging must not materialize bytes")

    class FakeArrowTable:
        @staticmethod
        def from_pandas(chunk: pd.DataFrame, preserve_index: bool) -> dict[str, int]:
            assert preserve_index is False
            return {"rows": len(chunk)}

    fake_pa = SimpleNamespace(Table=FakeArrowTable)

    monkeypatch.setattr(
        parquet_stage_module.tempfile,
        "SpooledTemporaryFile",
        FakeSpooledFile,
    )
    monkeypatch.setattr(
        parquet_stage_module,
        "write_arrow_table_to_parquet",
        lambda pq, arrow_table, spooled_file, row_group_size: None,
    )
    monkeypatch.setattr(
        parquet_stage_module,
        "upload_spooled_file",
        lambda fsspec_module, spooled_file, remote_uri: uploaded.append((remote_uri, spooled_file)),
    )

    rows = parquet_stage_module.write_dataframe_to_parquet_stage(
        pd.DataFrame({"id": [1, 2, 3]}),
        stage_external_location="s3://bucket/tmp/stage/",
        pa=fake_pa,
        pq=object(),
        fsspec_module=object(),
        row_group_size=2,
    )

    assert rows == 3
    assert max_active_spooled_files == 1
    assert active_spooled_files == 0
    assert [item[0] for item in uploaded] == [
        "s3://bucket/tmp/stage/part-00000.parquet",
        "s3://bucket/tmp/stage/part-00001.parquet",
    ]
    assert all(spooled_file.closed for _uri, spooled_file in uploaded)


def test_load_df_failure_cleanup_drops_only_target_absent_at_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dropped: list[str] = []
    options = SimpleNamespace(
        connection_backend="gp",
        connection_key="target_db",
        destination_table="sandbox.target",
        retry_cnt=1,
        timeout_increment=1,
        query_label=None,
    )

    monkeypatch.setattr(
        load_df_module,
        "_run_load_target_action",
        lambda _options, _role, operation: operation({"connection": FakeDbapiConnection()}),
    )
    monkeypatch.setattr(
        load_df_module,
        "drop_table_with_retry",
        lambda _backend, _key, _ref, table_name, **_kwargs: dropped.append(table_name),
    )

    load_df_module._cleanup_load(
        options,
        load_df_module.LoadState(
            target_exists=True,
            original_target_exists=False,
            target_created_by_operation=True,
        ),
        drop_created_target=True,
    )
    load_df_module._cleanup_load(
        options,
        load_df_module.LoadState(
            target_exists=True,
            original_target_exists=True,
            target_created_by_operation=True,
        ),
        drop_created_target=True,
    )

    assert dropped == ["sandbox.target"]


def test_load_df_rejects_non_dataframe_before_connection_lookup() -> None:
    with pytest.raises(TypeError, match="pandas DataFrame"):
        load_df_module.load_df("gp", "sandbox.target", [{"id": 1}])


def test_load_write_mode_and_clickhouse_shard_validation() -> None:
    assert (
        load_df_module._resolve_load_write_mode(
            "gp",
            append=False,
            write_mode=None,
        )
        == "replace"
    )
    assert (
        load_df_module._resolve_load_write_mode(
            "gp",
            append=True,
            write_mode=None,
        )
        == "append"
    )
    with pytest.raises(ValueError, match="append=True cannot be combined"):
        load_df_module._resolve_load_write_mode(
            "gp",
            append=True,
            write_mode="replace",
        )
    with pytest.raises(ValueError, match="ch_only_shard must be a boolean"):
        load_df_module._normalize_only_shard(1)


@pytest.mark.parametrize("write_mode", ["append", "upsert"])
def test_empty_existing_load_returns_metadata(write_mode: str) -> None:
    metadata = load_df_module.SqlOperationMetadata()
    result = load_df_module._handle_empty_dataframe_load(
        SimpleNamespace(
            append=write_mode == "append",
            write_mode=write_mode,
            destination_table="sandbox.target",
        ),
        load_df_module.LoadState(
            target_exists=True,
            original_target_exists=True,
        ),
        operation_metadata=metadata,
        return_metadata=True,
    )

    assert result.rows == 0
    assert result.metadata is metadata
    assert metadata.inserted_rows == 0
    assert metadata.affected_rows == 0


def test_empty_dataframe_cannot_create_or_replace_target() -> None:
    with pytest.raises(ValueError, match="empty DataFrame"):
        load_df_module._handle_empty_dataframe_load(
            SimpleNamespace(
                append=False,
                write_mode="replace",
                destination_table="sandbox.target",
            ),
            load_df_module.LoadState(
                target_exists=False,
                original_target_exists=False,
            ),
            operation_metadata=load_df_module.SqlOperationMetadata(),
            return_metadata=False,
        )


def test_dataframe_key_uniqueness_rejects_duplicate_keys() -> None:
    with pytest.raises(ValueError, match=r"Duplicate key values.*id, region"):
        load_df_module._validate_dataframe_key_uniqueness(
            pd.DataFrame(
                {
                    "id": [1, 1],
                    "region": ["north", "north"],
                }
            ),
            ["id", "region"],
        )


def test_dataframe_key_uniqueness_rejects_null_keys() -> None:
    with pytest.raises(ValueError, match=r"Null key values.*id"):
        load_df_module._validate_dataframe_key_uniqueness(
            pd.DataFrame({"id": [1, None], "value": ["one", "missing"]}),
            ["id"],
        )


@pytest.mark.parametrize(
    ("options", "target_types", "message"),
    [
        (
            {"s3_transfer_staging_schema": None, "s3_transfer_staging_location": "s3://x"},
            {"id": "BIGINT"},
            "s3_transfer_staging_schema",
        ),
        (
            {"s3_transfer_staging_schema": "stage", "s3_transfer_staging_location": None},
            {"id": "BIGINT"},
            "s3_transfer_staging_location",
        ),
        (
            {
                "s3_transfer_staging_schema": "stage",
                "s3_transfer_staging_location": "s3://x",
            },
            None,
            "Could not resolve target schema",
        ),
    ],
)
def test_create_load_parquet_stage_requires_complete_configuration(
    options: dict[str, object],
    target_types: dict[str, str] | None,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        load_df_module._create_load_parquet_stage_table(
            load_df_module.LoadOptions(
                connection_key="trino",
                connection_backend="trino",
                destination_table="sandbox.target",
                s3_transfer_staging_schema=options["s3_transfer_staging_schema"],
                s3_transfer_staging_location=options["s3_transfer_staging_location"],
            ),
            load_df_module.LoadState(
                target_exists=True,
                original_target_exists=True,
                target_column_types=target_types,
            ),
            object(),
        )


def test_cleanup_load_reports_each_best_effort_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    messages: list[str] = []
    monkeypatch.setattr(
        load_df_module,
        "_run_load_target_action",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("cleanup failed")),
    )
    monkeypatch.setattr(
        load_df_module,
        "cleanup_parquet_stage_location",
        lambda location: (_ for _ in ()).throw(RuntimeError("delete failed")),
    )
    monkeypatch.setattr(load_df_module, "time_print", messages.append)

    load_df_module._cleanup_load(
        load_df_module.LoadOptions(
            connection_key="gp",
            connection_backend="gp",
            destination_table="sandbox.target",
        ),
        load_df_module.LoadState(
            target_exists=True,
            original_target_exists=False,
            target_created_by_operation=True,
            overlap_stage_table="sandbox.overlap_stage",
            final_upsert_stage_table="sandbox.final_stage",
            stage_external_location="s3://bucket/stage/",
        ),
        drop_created_target=True,
    )

    assert messages == [
        "Failed to drop temporary load_df stage table sandbox.overlap_stage",
        "Failed to drop temporary load_df final upsert stage table sandbox.final_stage",
        "Failed to drop load_df target table sandbox.target created by this failed operation",
        "Failed to delete temporary load_df Parquet stage files s3://bucket/stage/",
    ]


def test_load_table_trino_scalar_normalization_handles_nat_and_target_types() -> None:
    assert load_sql_table_module._normalize_trino_value(pd.NaT, None) is None
    assert load_sql_table_module._normalize_trino_value("7", "bigint") == 7
    assert load_sql_table_module._normalize_trino_value(7, "varchar") == "7"


def test_load_lifecycle_remaining_validation_and_metadata_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    append_options = load_df_module.LoadOptions(
        connection_key="gp",
        connection_backend="gp",
        destination_table="sandbox.target",
        append=True,
    )
    state = load_df_module.LoadState(True, True)
    assert (
        load_df_module._handle_empty_dataframe_load(
            append_options,
            state,
            operation_metadata=load_df_module.SqlOperationMetadata(),
            return_metadata=False,
        )
        == 0
    )

    missing_distribution = load_df_module.LoadOptions(
        connection_key="gp",
        connection_backend="gp",
        destination_table="sandbox.target",
        gp_distributed_by_key=["missing"],
    )
    with pytest.raises(ValueError, match="missing"):
        load_df_module._validate_load_dataframe(
            missing_distribution,
            pd.DataFrame({"id": [1]}),
        )

    labelled = load_df_module.LoadOptions(
        connection_key="gp",
        connection_backend="gp",
        destination_table="sandbox.target",
        query_label="candidate-9",
    )
    create_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        load_df_module,
        "_create_sql_table_with_connection",
        lambda *_args, **kwargs: create_calls.append(kwargs),
    )
    monkeypatch.setattr(
        load_df_module.get_backend_adapter("gp"),
        "build_load_target_create_kwargs",
        lambda **_kwargs: {},
    )
    load_df_module._create_load_target_table(
        labelled,
        load_df_module.LoadState(False, False),
        object(),
        pd.DataFrame({"id": [1]}),
    )
    assert create_calls == [{"connection_key": "gp", "query_label": "candidate-9"}]

    analyze_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        load_df_module,
        "analyze_table",
        lambda **kwargs: analyze_calls.append(kwargs),
    )
    load_df_module._analyze_load_target(labelled, object())
    assert analyze_calls[0]["query_label"] == "candidate-9"

    monkeypatch.setattr(
        load_df_module,
        "_run_load_target_action",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("count failed")),
    )
    result_metadata = load_df_module._build_load_metadata(
        options=labelled,
        state=state,
        source_rows=2,
        inserted_rows=2,
        operation_metadata=load_df_module.SqlOperationMetadata(),
    )
    assert result_metadata.final_target_rows is None
    assert result_metadata.inserted_rows == 2


def test_load_lifecycle_stage_schema_parquet_guards_and_finalization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = load_df_module.LoadOptions(
        connection_key="trino",
        connection_backend="trino",
        destination_table="iceberg.sandbox.target",
        table_schema={"id": "BIGINT"},
        append=True,
        key_columns=["id"],
        transfer_staging_schema="scratch",
        s3_transfer_staging_location="s3://bucket/stage",
    )
    state = load_df_module.LoadState(True, True)
    stage_kwargs: list[dict[str, object]] = []
    monkeypatch.setattr(
        load_df_module,
        "_run_load_target_action",
        lambda _options, _role, operation: operation({"connection": object()}),
    )
    monkeypatch.setattr(
        load_df_module,
        "create_stage_table",
        lambda **kwargs: stage_kwargs.append(kwargs) or "scratch.stage",
    )
    monkeypatch.setattr(load_df_module, "insert_table_batch", lambda *_a, **_k: 1)
    monkeypatch.setattr(load_df_module, "validate_stage_target_key_overlap", lambda **_k: None)
    monkeypatch.setattr(load_df_module, "insert_from_table", lambda *_a, **_k: None)
    assert load_df_module._load_dataframe(options, state, pd.DataFrame({"id": [1]})) == 1
    assert stage_kwargs[0]["table_schema"] == {"id": "BIGINT"}

    monkeypatch.setattr(
        load_df_module,
        "ensure_parquet_staging_dependencies",
        lambda: (object(), object(), object()),
    )
    monkeypatch.setattr(load_df_module, "_create_load_parquet_stage_table", lambda *_a: None)
    with pytest.raises(RuntimeError, match="not initialized"):
        load_df_module._load_dataframe_via_parquet_stage(
            options=options,
            state=load_df_module.LoadState(True, True),
            df=pd.DataFrame({"id": [1]}),
            on_progress=None,
        )

    with pytest.raises(RuntimeError, match="was not initialized"):
        load_df_module._finalize_loaded_dataframe_stage(
            options=options,
            state=load_df_module.LoadState(True, True),
            connection=object(),
            df=pd.DataFrame({"id": [1]}),
        )

    overlap_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        load_df_module,
        "validate_stage_target_key_overlap",
        lambda **kwargs: overlap_calls.append(kwargs),
    )
    staged = load_df_module.LoadState(
        True,
        True,
        overlap_stage_table="scratch.stage",
    )
    load_df_module._finalize_loaded_dataframe_stage(
        options=options,
        state=staged,
        connection=object(),
        df=pd.DataFrame({"id": [1]}),
    )
    assert overlap_calls[0]["stage_table"] == "scratch.stage"


def test_load_parquet_collision_exhaustion_and_final_stage_noops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = load_df_module.LoadOptions(
        connection_key="trino",
        connection_backend="trino",
        destination_table="iceberg.sandbox.target",
            table_schema={"id": "BIGINT"},
            transfer_staging_schema="scratch",
            s3_transfer_staging_schema="scratch_s3",
            s3_transfer_staging_location="s3://bucket/stage",
    )
    state = load_df_module.LoadState(False, False)
    messages: list[str] = []
    monkeypatch.setattr(load_df_module, "STAGE_TABLE_NAME_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(
        load_df_module,
        "build_stage_table_name",
        lambda *_a, **_k: "scratch.collision",
    )
    monkeypatch.setattr(load_df_module, "table_exists", lambda *_a, **_k: True)
    monkeypatch.setattr(load_df_module, "time_print", messages.append)
    with pytest.raises(RuntimeError, match="unique Parquet"):
        load_df_module._create_load_parquet_stage_table(options, state, object())
    assert len(messages) == 2

    df = pd.DataFrame({"id": [1]})
    gp_options = load_df_module.LoadOptions(
        connection_key="gp",
        connection_backend="gp",
        destination_table="sandbox.target",
    )
    load_df_module._ensure_final_upsert_stage_table(gp_options, state, df)
    trino_state = load_df_module.LoadState(True, False)
    load_df_module._ensure_final_upsert_stage_table(options, trino_state, df)
    trino_state.original_target_exists = True
    trino_state.final_upsert_stage_table = "scratch.final"
    load_df_module._ensure_final_upsert_stage_table(options, trino_state, df)


def test_load_option_requirements_and_remaining_upsert_plan_branches(
    monkeypatch: pytest.MonkeyPatch,
    write_sql_connections: Any,
) -> None:
    with pytest.raises(ValueError, match="destination_table"):
        load_df_module._build_load_options("gp", " ", False, None, None, None)
    with pytest.raises(ValueError, match="upsert_partition_column"):
        load_df_module._build_load_options(
            "trino",
            "sandbox.target",
            False,
            "upsert",
            None,
            ["id"],
        )

    write_sql_connections(
        {
            "trino_no_template": {
                "type": "trino",
                "host": "trino.example",
                "port": 8080,
                "user": "user",
                "password": "password",
                "catalog": "iceberg",
                "schema": "sandbox",
            }
        }
    )
    with pytest.raises(ValueError, match="drop_sql_template"):
        load_df_module._build_load_options(
            "trino_no_template",
            "sandbox.target",
            False,
            "upsert",
            None,
            ["id"],
            upsert_partition_column="event_date",
        )

    gp_upsert = load_df_module.LoadOptions(
        connection_key="gp",
        connection_backend="gp",
        destination_table="sandbox.target",
        table_schema={"id": "BIGINT"},
        write_mode="upsert",
        key_columns=["id"],
        use_parquet_staging=True,
        transfer_staging_schema="scratch",
        s3_transfer_staging_location="s3://stage",
    )
    monkeypatch.setattr(load_df_module, "add_create_table_steps", lambda *_a, **_k: None)
    monkeypatch.setattr(load_df_module, "add_load_stage_step", lambda *_a, **_k: None)
    monkeypatch.setattr(load_df_module, "build_upsert_stage_sqls", lambda *_a, **_k: [])
    monkeypatch.setattr(load_df_module, "add_cleanup_stage_step", lambda *_a, **_k: None)
    load_df_module._add_parquet_load_plan_steps(
        load_df_module.SqlPlan(operation="load"),
        gp_upsert,
        pd.DataFrame({"id": [1]}),
        load_df_module.SqlOperationMetadata(),
    )

    monkeypatch.setattr(load_df_module, "validate_stage_uniqueness", lambda **_k: None)
    monkeypatch.setattr(load_df_module, "insert_from_table", lambda *_a, **_k: None)
    load_df_module._finalize_loaded_dataframe_stage(
        options=gp_upsert,
        state=load_df_module.LoadState(
            True,
            False,
            overlap_stage_table="scratch.stage",
        ),
        connection=object(),
        df=pd.DataFrame({"id": [1]}),
    )
