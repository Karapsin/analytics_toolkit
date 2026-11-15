from __future__ import annotations

import importlib
import json
from contextlib import nullcontext
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from analytics_toolkit.general import set_connections_path
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from analytics_toolkit.sql.backends.ch.reconfigure_ddl import parse_create_table
from sqlglot import exp, parse_one

from tests.sql._support.assertions import strip_query_label as _strip_query_label
from tests.sql._support.ch_drop_scope import DropScopeState, _DropConnection

ch_ctas_module = importlib.import_module("analytics_toolkit.sql.backends.ch.create_table_as")
ch_ctas_impl = ch_ctas_module
sql_module = importlib.import_module("analytics_toolkit.sql")


TARGET_TABLE = "default.events_result"
TARGET_SHARD_TABLE = "default.events_result_shard"
QUERY = """
SELECT
    dt,
    id,
    amount
FROM default.events_source
WHERE amount > 0
""".strip()
CTE_JOIN_QUERY = """
WITH trigger_map AS (
    SELECT 1 AS id
)
SELECT
    events.id,
    trigger_map.kind
FROM default.events_source AS events
LEFT JOIN trigger_map AS trigger_map
    ON events.id = trigger_map.id
""".strip()


class FakeClickHouseResult:
    def __init__(
        self,
        result_rows: list[tuple[Any, ...]],
        column_names: tuple[str, ...] = (),
        column_types: tuple[Any, ...] = (),
    ) -> None:
        self.result_rows = result_rows
        self.column_names = column_names
        self.column_types = column_types


class FakeDatabaseError(Exception):
    pass


class FakeClickHouseClient:
    def __init__(self) -> None:
        self.commands: list[str] = []
        self.command_settings: list[dict[str, object] | None] = []
        self.queries: list[str] = []
        self.created_tables: set[str] = set()
        self.close_calls = 0
        self.insert_error: Exception | None = None
        self.schema_query_error: Exception | None = None
        self.table_ddls: dict[str, str] = {}
        self.data_inserts: list[tuple[str, list[str], list[tuple[Any, ...]]]] = []
        self.ownership_connection: _DropConnection | None = None

    def command(
        self,
        sql: str,
        settings: dict[str, object] | None = None,
    ) -> None:
        self.commands.append(sql)
        self.command_settings.append(settings)
        self._track_table_ddl(sql)
        body = _strip_query_label(sql)
        if self.ownership_connection is not None and body.startswith(
            ("CREATE TABLE", "DROP TABLE")
        ):
            self.ownership_connection.command(sql, settings=settings)
        if self.insert_error is not None and sql.startswith("INSERT INTO"):
            raise self.insert_error

    def query(self, sql: str, **_kwargs: Any) -> FakeClickHouseResult:
        self.queries.append(sql)
        body = _strip_query_label(sql)
        if body.startswith("SELECT getMacro("):
            return FakeClickHouseResult([("core",)])
        if body.startswith("SELECT *\nFROM (\n"):
            if self.schema_query_error is not None:
                raise self.schema_query_error
            return FakeClickHouseResult(
                [],
                column_names=("dt", "id", "amount"),
                column_types=(
                    type("FakeType", (), {"name": "Date"})(),
                    type("FakeType", (), {"name": "UInt64"})(),
                    type("FakeType", (), {"name": "Decimal(18, 4)"})(),
                ),
            )
        if body.startswith("DESCRIBE TABLE ("):
            if self.schema_query_error is not None:
                raise self.schema_query_error
            return FakeClickHouseResult(
                [("dt", "Date"), ("id", "UInt64"), ("amount", "Decimal(18, 4)")]
            )
        if self.ownership_connection is not None:
            result = self.ownership_connection.query(sql, **_kwargs)
            return FakeClickHouseResult(
                result.result_rows,
                column_names=result.column_names,
                column_types=result.column_types,
            )
        return self._legacy_query(body)

    def _legacy_query(self, body: str) -> FakeClickHouseResult:
        if body.startswith("SHOW CREATE TABLE "):
            name = body.removeprefix("SHOW CREATE TABLE ").replace("`", "")
            return FakeClickHouseResult([(self.table_ddls[name],)])
        if body.startswith("SELECT create_table_query FROM clusterAllReplicas("):
            return FakeClickHouseResult([(self.table_ddls[TARGET_SHARD_TABLE],)])
        return self._metadata_result(body)

    def _metadata_result(self, sql: str) -> FakeClickHouseResult:
        if "clusterAllReplicas" in sql and "system, one" in sql:
            return FakeClickHouseResult([(1,)])
        if "FROM system.clusters" in sql:
            return FakeClickHouseResult([(1,)])
        if "clusterAllReplicas" in sql and "system, tables" in sql:
            return FakeClickHouseResult([(self._cluster_table_count(sql),)])
        if "clusterAllReplicas" in sql and "system, columns" in sql:
            return FakeClickHouseResult([(sql.count("name = ") or 1,)])
        if "clusterAllReplicas" in sql:
            return FakeClickHouseResult([(1,)])
        if sql.startswith("EXISTS TABLE "):
            table_name = sql.removeprefix("EXISTS TABLE ").strip().replace("`", "")
            return FakeClickHouseResult([(int(table_name in self.created_tables),)])
        message = f"Unexpected query: {sql}"
        raise AssertionError(message)

    def query_df_stream(self, query: str) -> Any:
        self.queries.append(query)
        frame = pd.DataFrame({"dt": [date(2026, 1, 1)], "id": [1], "amount": [Decimal("1.2500")]})
        return nullcontext(iter([frame]))

    def insert_df(self, table: str, df: pd.DataFrame, column_names: list[str]) -> None:
        self.data_inserts.append((table, column_names, list(df.itertuples(index=False, name=None))))
        if self.insert_error is not None:
            raise self.insert_error

    def insert(self, table: str, data: Any, column_names: list[str], **_kwargs: Any) -> None:
        self.data_inserts.append((table, column_names, [tuple(row) for row in data]))
        if self.insert_error is not None:
            raise self.insert_error

    def close(self) -> None:
        self.close_calls += 1
        if self.ownership_connection is not None:
            self.ownership_connection.close()

    def _track_table_ddl(self, sql: str) -> None:
        body = _strip_query_label(sql)
        for prefix in ("CREATE TABLE IF NOT EXISTS ", "CREATE OR REPLACE TABLE ", "CREATE TABLE "):
            if body.startswith(prefix):
                name = body[len(prefix) :].split()[0].replace("`", "")
                self.created_tables.add(name)
                self.table_ddls[name] = "CREATE TABLE " + body[len(prefix) :]
                return
        if body.startswith("DROP TABLE IF EXISTS "):
            name = body.removeprefix("DROP TABLE IF EXISTS ").split()[0].replace("`", "")
            self.created_tables.discard(name)
            self.table_ddls.pop(name, None)

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


@pytest.fixture
def fake_client(monkeypatch: pytest.MonkeyPatch) -> FakeClickHouseClient:
    path = Path(".connections")
    config = json.loads(path.read_text())
    config["ch"]["transfer_staging_schema"] = None
    path.write_text(json.dumps(config))
    set_connections_path(path)
    client = FakeClickHouseClient()
    monkeypatch.setattr(
        ch_ctas_module,
        "get_sql_connection",
        lambda connection_key: client,
    )
    return client


def test_ch_create_table_as_is_exported() -> None:
    assert "ch_create_table_as" not in sql_module.__all__
    assert not hasattr(sql_module, "ch_create_table_as")


def test_ch_create_table_as_creates_pair_and_inserts_query(
    fake_client: FakeClickHouseClient,
) -> None:
    ch_ctas_module.ch_create_table_as(
        "ch",
        TARGET_TABLE,
        QUERY + ";",
        partition_by=["dt"],
        order_by=["dt", "id"],
        ch_sharding_key="cityHash64(dt, id)",
    )

    assert not any(command.startswith("DROP TABLE") for command in fake_client.commands)
    shard_sql, local_shard_sql, distributed_sql, local_distributed_sql = fake_client.commands[:4]
    assert shard_sql.startswith(f"CREATE TABLE {TARGET_SHARD_TABLE}")
    assert "ON CLUSTER '{cluster}'" in shard_sql
    assert "ENGINE = ReplicatedMergeTree" in shard_sql
    assert "PARTITION BY `dt`" in shard_sql
    assert "ORDER BY (`dt`, `id`)" in shard_sql
    assert "`dt` Date" in shard_sql
    assert "`id` UInt64" in shard_sql
    assert "`amount` Decimal(18, 4)" in shard_sql
    assert QUERY not in shard_sql
    assert local_shard_sql.startswith(f"CREATE TABLE IF NOT EXISTS {TARGET_SHARD_TABLE}")
    assert "ON CLUSTER" not in local_shard_sql
    assert "UUID '" in local_shard_sql
    assert "ENGINE = ReplicatedMergeTree" in local_shard_sql
    assert distributed_sql.startswith(f"CREATE TABLE {TARGET_TABLE}")
    assert "ON CLUSTER '{cluster}'" in distributed_sql
    assert "ENGINE = Distributed(" in distributed_sql
    assert "    '{cluster}'," in distributed_sql
    assert "    'default'," in distributed_sql
    assert "    'events_result_shard'," in distributed_sql
    assert "    cityHash64(dt, id)" in distributed_sql
    assert "`amount` Decimal(18, 4)" in distributed_sql
    assert QUERY not in distributed_sql
    assert local_distributed_sql.startswith(f"CREATE TABLE IF NOT EXISTS {TARGET_TABLE}")
    assert "ON CLUSTER" not in local_distributed_sql
    assert fake_client.commands[4] == f"INSERT INTO {TARGET_TABLE}\n{QUERY}"
    assert fake_client.command_settings[0] == {
        "distributed_ddl_task_timeout": 0,
        "distributed_ddl_output_mode": "none",
    }
    assert f"DESCRIBE TABLE ({QUERY})" in fake_client.queries
    assert f"EXISTS TABLE {TARGET_TABLE}" in fake_client.queries
    assert f"EXISTS TABLE {TARGET_SHARD_TABLE}" in fake_client.queries
    assert any("clusterAllReplicas" in query for query in fake_client.queries)
    assert fake_client.close_calls == 1


def test_ch_create_table_as_recreates_only_verified_existing_pair(
    fake_client: FakeClickHouseClient,
) -> None:
    path = Path(".connections")
    config = json.loads(path.read_text())
    config["ch"]["host"] = "coordinator"
    path.write_text(json.dumps(config))
    set_connections_path(path)
    state = DropScopeState()
    state.clusters.update(core=(state.coordinator,))
    state.clusters["{cluster}"] = (state.coordinator,)
    columns = "(`dt` Date, `id` UInt64, `amount` Decimal(18, 4))"
    fake_client.created_tables.update((TARGET_TABLE, TARGET_SHARD_TABLE))
    fake_client.table_ddls.update(
        {
            TARGET_TABLE: (
                f"CREATE TABLE {TARGET_TABLE} {columns} "
                "ENGINE = Distributed('{cluster}', 'default', 'events_result_shard', rand())"
            ),
            TARGET_SHARD_TABLE: (
                f"CREATE TABLE {TARGET_SHARD_TABLE} {columns} "
                "ENGINE = ReplicatedMergeTree ORDER BY id"
            ),
        }
    )
    for name, ddl in fake_client.table_ddls.items():
        state._create(state.coordinator, parse_create_table(ddl, name))
    foreign = parse_create_table(
        f"CREATE TABLE {TARGET_TABLE} {columns} ENGINE=MergeTree ORDER BY id", TARGET_TABLE
    )
    state._create(state.remote, foreign, [(date(2026, 1, 1), 100, Decimal("9.0000"))])
    foreign_physical = parse_create_table(
        f"CREATE TABLE {TARGET_SHARD_TABLE} {columns} ENGINE=MergeTree ORDER BY id",
        TARGET_SHARD_TABLE,
    )
    state._create(state.remote, foreign_physical, [(date(2026, 1, 1), 101, Decimal("8.0000"))])
    before = state.snapshot()
    fake_client.ownership_connection = _DropConnection(state, state.coordinator, native=False)
    ch_ctas_module.ch_create_table_as("ch", TARGET_TABLE, QUERY)

    drops = [
        parse_one(_strip_query_label(statement), read=ClickHouseDialect)
        for statement in fake_client.commands[:2]
    ]
    assert all(isinstance(statement, exp.Drop) for statement in drops)
    assert [
        (table.db, table.name, statement.find(exp.OnCluster).this.name)
        for statement in drops
        for table in statement.args.get("tables") or [statement.this]
    ] == [("default", "events_result", "core"), ("default", "events_result_shard", "core")]
    assert len(fake_client.commands) == 7
    assert fake_client.commands[2].startswith(f"CREATE TABLE {TARGET_SHARD_TABLE}")
    assert fake_client.commands[-1] == f"INSERT INTO {TARGET_TABLE}\n{QUERY}"
    assert f"SHOW CREATE TABLE {TARGET_TABLE}" in fake_client.queries
    assert any(query.startswith("SELECT create_table_query") for query in fake_client.queries)
    assert state.snapshot()[state.remote] == before[state.remote]
    for table in (TARGET_TABLE, TARGET_SHARD_TABLE):
        assert (
            state.host_tables[state.coordinator][table].uuid
            != before[state.coordinator][table]["uuid"]
        )
    assert {
        column: exp.DataType.build(native, dialect=ClickHouseDialect)
        for column, native in state.host_tables[state.coordinator][TARGET_TABLE].schema.items()
    } == {
        column: exp.DataType.build(native, dialect=ClickHouseDialect)
        for column, native in {"dt": "Date", "id": "UInt64", "amount": "Decimal(18, 4)"}.items()
    }
    assert state.host_tables[state.coordinator][TARGET_SHARD_TABLE].schema == (
        state.host_tables[state.coordinator][TARGET_TABLE].schema
    )
    assert any(settings == {"skip_unavailable_shards": 0} for settings in state.query_settings)
    assert fake_client.close_calls == 1


def test_ch_create_table_as_table_schema_overrides_inferred_types(
    fake_client: FakeClickHouseClient,
) -> None:
    ch_ctas_module.ch_create_table_as(
        "ch",
        TARGET_TABLE,
        QUERY,
        table_schema={"dt": "String", "id": "String", "amount": "Float64"},
    )

    shard_sql = fake_client.commands[0]
    assert "`dt` String" in shard_sql
    assert "`id` String" in shard_sql
    assert "`amount` Float64" in shard_sql
    assert "`dt` Date" not in shard_sql
    assert f"DESCRIBE TABLE ({QUERY})" in fake_client.queries
    assert fake_client.data_inserts[-1][2] == [(b"2026-01-01", b"1", 1.25)]


def test_ch_create_table_as_dry_run_uses_table_schema() -> None:
    plan = ch_ctas_module.ch_create_table_as(
        "ch",
        TARGET_TABLE,
        QUERY,
        dry_run=True,
        table_schema={"dt": "Date", "id": "UInt64", "amount": "Decimal(18, 4)"},
    )

    create_sql = next(
        statement.sql for statement in plan.statements if statement.phase == "create_target"
    )
    assert plan.options["table_schema"] == {
        "dt": "Date",
        "id": "UInt64",
        "amount": "Decimal(18, 4)",
    }
    assert "`dt` Date" in create_sql
    assert "`id` UInt64" in create_sql
    assert "`amount` Decimal(18, 4)" in create_sql


def test_ch_create_table_as_skips_insert_when_insert_data_false(
    fake_client: FakeClickHouseClient,
) -> None:
    ch_ctas_module.ch_create_table_as(
        "ch",
        TARGET_TABLE,
        QUERY,
        insert_data=False,
    )

    assert len(fake_client.commands) == 4
    assert not any(command.startswith("INSERT INTO ") for command in fake_client.commands)


def test_ch_create_table_as_skips_drop_when_drop_target_if_exists_false(
    fake_client: FakeClickHouseClient,
) -> None:
    ch_ctas_module.ch_create_table_as(
        "ch",
        TARGET_TABLE,
        QUERY,
        drop_target_if_exists=False,
        table_schema={"dt": "Date", "id": "UInt64", "amount": "Decimal(18, 4)"},
    )

    assert not any(command.startswith("DROP TABLE") for command in fake_client.commands)
    assert fake_client.commands[0].startswith(f"CREATE TABLE {TARGET_SHARD_TABLE}")
    assert fake_client.commands[-1] == f"INSERT INTO {TARGET_TABLE}\n{QUERY}"


def test_ch_create_table_as_dry_run_omits_insert_when_insert_data_false() -> None:
    plan = ch_ctas_module.ch_create_table_as(
        "ch",
        TARGET_TABLE,
        QUERY,
        dry_run=True,
        insert_data=False,
        table_schema={"dt": "Date", "id": "UInt64", "amount": "Decimal(18, 4)"},
    )

    assert "insert_target" not in [statement.phase for statement in plan.statements]
    assert not any(sql.startswith("INSERT INTO ") for sql in plan.sqls)


def test_ch_create_table_as_quotes_cluster_macro(
    fake_client: FakeClickHouseClient,
) -> None:
    ch_ctas_module.ch_create_table_as(
        "ch",
        TARGET_TABLE,
        QUERY,
    )

    assert not any(command.startswith("DROP TABLE") for command in fake_client.commands)
    shard_sql, _, distributed_sql, _ = fake_client.commands[:4]
    assert "ON CLUSTER '{cluster}'" in shard_sql
    assert "ON CLUSTER '{cluster}'" in distributed_sql
    assert "    '{cluster}'," in distributed_sql


def test_ch_create_table_as_rejects_multiple_statements(
    fake_client: FakeClickHouseClient,
) -> None:
    with pytest.raises(
        ch_ctas_module.InvalidSqlInputError,
        match="exactly one SQL statement",
    ):
        ch_ctas_module.ch_create_table_as(
            "ch",
            TARGET_TABLE,
            "SELECT 1; SELECT 2",
        )

    assert fake_client.commands == []
    assert fake_client.close_calls == 0


def test_ch_create_table_as_rejects_non_clickhouse_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        ch_ctas_module,
        "get_sql_connection",
        lambda connection_key: pytest.fail("connection should not be opened"),
    )

    with pytest.raises(
        ch_ctas_module.UnsupportedConnectionTypeError,
        match="requires a ch",
    ):
        ch_ctas_module.ch_create_table_as("gp", TARGET_TABLE, QUERY)


def test_ch_create_table_as_final_insert_cte_unknown_table_keeps_exception_type(
    fake_client: FakeClickHouseClient,
) -> None:
    error = FakeDatabaseError(
        "Code: 60. DB::Exception: Table default.trigger_map does not exist. (UNKNOWN_TABLE)"
    )
    fake_client.insert_error = error

    with pytest.raises(FakeDatabaseError) as exc_info:
        ch_ctas_module.ch_create_table_as("ch", TARGET_TABLE, CTE_JOIN_QUERY)

    assert exc_info.value is error
    notes = "\n".join(getattr(exc_info.value, "__notes__", ()))
    assert "ClickHouse could not resolve CTE 'trigger_map'" in notes
    assert "GLOBAL LEFT JOIN trigger_map AS alias" in notes


def test_ch_create_table_as_final_insert_unknown_table_without_cte_has_no_hint(
    fake_client: FakeClickHouseClient,
) -> None:
    error = FakeDatabaseError(
        "Code: 60. DB::Exception: Table default.missing_map does not exist. (UNKNOWN_TABLE)"
    )
    fake_client.insert_error = error

    with pytest.raises(FakeDatabaseError) as exc_info:
        ch_ctas_module.ch_create_table_as("ch", TARGET_TABLE, CTE_JOIN_QUERY)

    assert exc_info.value is error
    notes = "\n".join(getattr(exc_info.value, "__notes__", ()))
    assert "GLOBAL LEFT JOIN" not in notes


def test_ch_create_table_as_schema_inference_cte_unknown_table_adds_hint(
    fake_client: FakeClickHouseClient,
) -> None:
    error = FakeDatabaseError(
        "Code: 60. DB::Exception: Table default.trigger_map does not exist. (UNKNOWN_TABLE)"
    )
    fake_client.schema_query_error = error

    with pytest.raises(FakeDatabaseError) as exc_info:
        ch_ctas_module.ch_create_table_as("ch", TARGET_TABLE, CTE_JOIN_QUERY)

    assert exc_info.value is error
    notes = "\n".join(getattr(exc_info.value, "__notes__", ()))
    assert "ClickHouse could not resolve CTE 'trigger_map'" in notes
    assert "GLOBAL LEFT JOIN trigger_map AS alias" in notes


def test_ch_create_table_as_only_shard_executes_drop_create_and_metadata(
    fake_client: FakeClickHouseClient,
) -> None:
    result = ch_ctas_module.ch_create_table_as(
        "ch",
        TARGET_TABLE,
        QUERY,
        ch_only_shard=True,
        return_metadata=True,
        table_schema={"dt": "Date", "id": "UInt64", "amount": "Float64"},
    )
    assert fake_client.commands[0].startswith(f"CREATE TABLE {TARGET_TABLE}")
    assert fake_client.data_inserts == [
        (TARGET_TABLE, ["dt", "id", "amount"], [(date(2026, 1, 1), 1, 1.25)])
    ]
    assert not any(command.startswith("DROP TABLE") for command in fake_client.commands)
    assert result.metadata.statement_count == 2
    assert fake_client.close_calls == 1


def test_ch_create_table_as_explicit_schema_failure_adds_cte_note(
    fake_client: FakeClickHouseClient,
) -> None:
    error = FakeDatabaseError("Code: 60. Table default.trigger_map does not exist. (UNKNOWN_TABLE)")
    fake_client.schema_query_error = error
    with pytest.raises(FakeDatabaseError) as exc_info:
        ch_ctas_module.ch_create_table_as(
            "ch",
            TARGET_TABLE,
            CTE_JOIN_QUERY,
            table_schema={"id": "UInt64", "kind": "String"},
        )
    assert "ClickHouse could not resolve CTE 'trigger_map'" in "\n".join(
        getattr(exc_info.value, "__notes__", ())
    )
    assert fake_client.close_calls == 1


def test_ch_create_table_as_dry_schema_placeholders_and_validation() -> None:
    local_plan = ch_ctas_module.ch_create_table_as(
        "ch",
        TARGET_TABLE,
        QUERY,
        ch_only_shard=True,
        dry_run=True,
    )
    assert local_plan.sqls[1].startswith(f"CREATE TABLE {TARGET_TABLE}")
    assert "<query schema>" in local_plan.sqls[1]
    assert local_plan.options["pending_schema_inspection"] is True
    assert local_plan.options["pending_existing_layout_verification"] is True
    distributed_plan = ch_ctas_module.ch_create_table_as(
        "ch",
        TARGET_TABLE,
        QUERY,
        dry_run=True,
    )
    assert sum("<query schema>" in sql for sql in distributed_plan.sqls) == 4
    with pytest.raises(ValueError, match="ch_only_shard must be a boolean"):
        ch_ctas_impl.build_ch_create_table_as_sqls(
            TARGET_TABLE,
            "`id` UInt64",
            QUERY,
            ch_only_shard=1,
        )
    with pytest.raises(ch_ctas_module.InvalidSqlInputError, match="must not be empty"):
        ch_ctas_module.ch_create_table_as("ch", TARGET_TABLE, "   ")


def test_ch_create_table_as_schema_result_validation_and_type_fallback() -> None:
    class ResultConnection:
        def __init__(self, result: FakeClickHouseResult) -> None:
            self.result = result

        def query(self, _sql: str) -> FakeClickHouseResult:
            return self.result

    with pytest.raises(ValueError, match="at least one column"):
        ch_ctas_impl._infer_ch_query_columns(
            ResultConnection(FakeClickHouseResult([])),
            QUERY,
        )
    with pytest.raises(ValueError, match="Could not infer"):
        ch_ctas_impl._infer_ch_query_columns(
            ResultConnection(FakeClickHouseResult([], column_names=("id",), column_types=())),
            QUERY,
        )
    with pytest.raises(ValueError, match="at least one column"):
        ch_ctas_impl._inspect_ch_query_column_names(
            ResultConnection(FakeClickHouseResult([])),
            QUERY,
        )
    assert ch_ctas_impl._ch_type_name("UInt64") == "UInt64"


def test_ch_create_table_as_explicit_types_missing_blank_and_valid(
    fake_client: FakeClickHouseClient,
) -> None:
    with pytest.raises(ValueError, match=r"missing SQL type for column.*amount"):
        ch_ctas_module.ch_create_table_as(
            "ch",
            TARGET_TABLE,
            QUERY,
            table_schema={"dt": "Date", "id": "UInt64"},
        )
    assert fake_client.close_calls == 1

    with pytest.raises(ValueError, match="SQL type for column 'id' must not be empty"):
        ch_ctas_module.ch_create_table_as(
            "ch",
            TARGET_TABLE,
            QUERY,
            table_schema={"dt": "Date", "id": "  ", "amount": "Float64"},
        )
    assert fake_client.close_calls == 1

    definitions = ch_ctas_impl.build_table_schema_column_definitions(
        "ch",
        {"dt": "Date", "id": "UInt64", "amount": "Float64"},
        columns=["dt", "id", "amount"],
    )
    assert definitions == "`dt` Date, `id` UInt64, `amount` Float64"


def test_ch_create_table_as_cte_parser_matching_and_note_edges() -> None:
    assert ch_ctas_impl._extract_query_cte_names("not valid (") == {}
    scalar = "WITH one AS 1 SELECT one"
    assert ch_ctas_impl._extract_query_cte_names(scalar) == {}
    assert ch_ctas_impl._clickhouse_missing_table_names(RuntimeError("ordinary")) == []
    assert ch_ctas_impl._clickhouse_missing_table_names(
        RuntimeError(
            "UNKNOWN_TABLE: Table `default`.`trigger_map` does not exist; "
            "Table db.other does not exist"
        )
    ) == ["trigger_map", "other"]
    error = RuntimeError("Table default.trigger_map does not exist")
    ch_ctas_impl._annotate_ch_cte_join_exception(error, CTE_JOIN_QUERY, backend="gp")
    assert getattr(error, "__notes__", ()) == ()
    ch_ctas_impl._annotate_ch_cte_join_exception(
        error,
        "SELECT * FROM missing",
        backend="ch",
    )
    assert getattr(error, "__notes__", ()) == ()

    note = ch_ctas_impl._ch_cte_join_note("trigger_map")
    ch_ctas_impl._add_exception_note_once(error, note)
    ch_ctas_impl._add_exception_note_once(error, note)
    assert getattr(error, "__notes__", ()).count(note) == 1

    fallback_error = RuntimeError("fallback")
    fallback_error.add_note = None  # type: ignore[method-assign]
    ch_ctas_impl._add_exception_note_once(fallback_error, "fallback note")
    assert fallback_error.__notes__ == ["fallback note"]
