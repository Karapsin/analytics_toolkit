from __future__ import annotations

# ruff: noqa: C901, EM101, PT030, TRY003
import importlib
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends.ch.creation_policy import (
    ClickHouseCreationPolicy,
    build_policy_create_as_sqls,
)
from analytics_toolkit.sql.connection.errors import InvalidSqlInputError
from analytics_toolkit.sql.execution.plans import SqlPlan, SqlStatement

ddl_api_module = importlib.import_module("analytics_toolkit.sql.ddl.api")
ddl_module = importlib.import_module("analytics_toolkit.sql.ddl")
execute_create_module = importlib.import_module("analytics_toolkit.sql.dml.io.execute_create")
query_writes_module = importlib.import_module("analytics_toolkit.sql.dml.io.query_writes")


class _ExecuteCreateConnection:
    def __init__(self, *, close_error: bool = False) -> None:
        self.close_error = close_error
        self.closed = 0
        self.rolled_back = 0

    def rollback(self) -> None:
        self.rolled_back += 1

    def close(self) -> None:
        self.closed += 1
        if self.close_error:
            raise OSError("close failed")


class _ExecuteCreateAdapter:
    supports_transactions = False
    requires_execute_create_schema_inference = False

    def __init__(self, *, failure: Exception | None = None) -> None:
        self.failure = failure
        self.events: list[Any] = []

    def table_exists(self, *_args: Any, **_kwargs: Any) -> bool:
        self.events.append("exists")
        return False

    def execute_sql(self, _connection: Any, query: str, **_kwargs: Any) -> None:
        self.events.append(("setup", query))

    def execute_commands(self, _connection: Any, commands: list[str]) -> None:
        self.events.append(("commands", commands))

    def execute_command(self, _connection: Any, command: str) -> Any:
        self.events.append(("command", command))
        if self.failure is not None:
            raise self.failure
        return SimpleNamespace(rowcount=7)

    def after_create_table(self, *_args: Any, **_kwargs: Any) -> None:
        self.events.append("after_create")

    def prepare_sql(self, _config: Any, statement: str) -> str:
        return statement

    def rollback_quietly(self, connection: _ExecuteCreateConnection) -> None:
        connection.rollback()


def _execute_create_options(**overrides: Any) -> Any:
    values = {
        "connection_key": "gp",
        "backend": "gp",
        "table_name": "mart.target",
        "setup_sqls": [],
        "source_sql": "SELECT 1 AS id",
        "create_sqls": ["CREATE TABLE mart.target AS SELECT 1 AS id"],
        "drop_sqls": [],
        "insert_after_create": False,
        "gp_distributed_by_key": None,
        "gp_partitions": None,
        "partition_by": None,
        "order_by": None,
        "ddl_properties": None,
        "ch_creation_policy": None,
        "ch_only_shard": False,
        "drop_if_exists": False,
        "if_not_exists": False,
        "print_queries": False,
        "gp_break_query": False,
        "gp_commit_each_statement": False,
        "retry_cnt": 1,
        "timeout_increment": 0,
        "query_label": None,
        "return_metadata": False,
        "progress": False,
        "retry_policy": "safe",
    }
    values.update(overrides)
    return execute_create_module._ExecuteCreateOptions(**values)


def test_insert_builds_positional_insert_plan() -> None:
    plan = sql.insert(
        "gp",
        "mart.target",
        "SELECT id, amount FROM staging.source",
        dry_run=True,
    )

    assert plan.operation == "insert"
    assert plan.target_table == "mart.target"
    assert plan.sqls == ["INSERT INTO mart.target\nSELECT id, amount FROM staging.source"]
    assert plan.statements[0].phase == "insert_target"


def test_execute_insert_rewrites_only_final_statement() -> None:
    plan = sql.execute_insert(
        "trino",
        "mart.target",
        "CREATE TABLE scratch.source AS SELECT 1 AS id; SELECT id FROM scratch.source",
        dry_run=True,
        query_label="write-test",
    )

    assert plan.operation == "execute_insert"
    assert [statement.phase for statement in plan.statements] == [
        "setup",
        "insert_target",
    ]
    assert plan.sqls[-1].endswith("INSERT INTO mart.target\nSELECT id FROM scratch.source")
    assert sum("query_label=write-test" in statement for statement in plan.sqls) == 2


def test_query_write_helpers_require_final_select() -> None:
    with pytest.raises(InvalidSqlInputError, match="final statement"):
        sql.execute_insert("gp", "mart.target", "DELETE FROM scratch.source")
    with pytest.raises(InvalidSqlInputError, match="exactly one"):
        sql.insert("gp", "mart.target", "SELECT 1; SELECT 2")


def test_insert_returns_backend_affected_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        query_writes_module,
        "_execute_sql_options",
        lambda _options: SimpleNamespace(rowcount=7),
    )

    result = sql.insert(
        "gp",
        "mart.target",
        "SELECT id FROM staging.source",
        return_metadata=True,
    )

    assert result.rows == 7
    assert result.affected_rows == 7
    assert result.inserted_rows == 7


def test_execute_create_uses_backend_ddl_defaults_in_dry_run() -> None:
    gp_plan = sql.execute_create(
        "gp",
        "mart.target",
        "SELECT 1 AS id",
        dry_run=True,
    )
    trino_plan = sql.execute_create(
        "trino",
        "mart.target",
        "SELECT 1 AS id",
        dry_run=True,
    )

    assert gp_plan.sqls == [
        "CREATE TABLE mart.target WITH (appendonly=true, blocksize=32768, "
        "compresstype=zstd, compresslevel=4, orientation=column) AS "
        "SELECT 1 AS id DISTRIBUTED RANDOMLY"
    ]
    assert trino_plan.sqls == [
        "CREATE TABLE mart.target WITH (format = 'PARQUET', "
        "object_store_layout_enabled = true) AS SELECT 1 AS id"
    ]


def test_execute_create_clickhouse_pair_uses_empty_as_and_one_facade_insert() -> None:
    plan = sql.execute_create(
        "ch",
        "mart.target",
        "SELECT 1 AS id",
        dry_run=True,
    )

    create_sqls = [
        statement.sql for statement in plan.statements if statement.phase == "create_table"
    ]
    insert_sqls = [
        statement.sql for statement in plan.statements if statement.phase == "insert_target"
    ]
    assert len(create_sqls) == 4
    assert all("EMPTY AS SELECT 1 AS id" in statement for statement in create_sqls)
    assert insert_sqls == ["INSERT INTO mart.target\nSELECT 1 AS id"]


def test_execute_create_validates_create_modes() -> None:
    with pytest.raises(InvalidSqlInputError, match="cannot both"):
        sql.execute_create(
            "gp",
            "mart.target",
            "SELECT 1",
            drop_if_exists=True,
            if_not_exists=True,
        )


def test_execute_create_if_not_exists_skips_setup_on_same_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[Any] = []

    class FakeConnection:
        def close(self) -> None:
            events.append("close")

    class FakeAdapter:
        backend = "gp"
        supports_transactions = False
        requires_execute_create_schema_inference = False

        def normalize_ch_columns_or_expression(self, value: Any, _name: str) -> Any:
            return value

        def normalize_gp_partitions_option(self, value: Any, **_kwargs: Any) -> Any:
            return value

        def validate_gp_distributed_by_key_option(self, *_args: Any, **_kwargs: Any) -> None:
            return None

        def validate_trino_insert_chunk_size_option(self, *_args: Any, **_kwargs: Any) -> None:
            return None

        def validate_ch_create_table_options(self, **_kwargs: Any) -> None:
            return None

        def build_execute_create_as_sqls(self, **kwargs: Any) -> tuple[list[str], bool]:
            return [f"CREATE TABLE IF NOT EXISTS {kwargs['table_name']} AS SELECT 1"], False

        def build_drop_target_sqls(self, *_args: Any, **_kwargs: Any) -> list[str]:
            return []

        def prepare_sql(self, _config: Any, statement: str) -> str:
            return statement

        def table_exists(self, *_args: Any, **_kwargs: Any) -> bool:
            events.append("exists")
            return True

        def rollback_quietly(self, _connection: Any) -> None:
            return None

    adapter = FakeAdapter()
    monkeypatch.setattr(execute_create_module, "get_backend_adapter", lambda _backend: adapter)
    monkeypatch.setattr(execute_create_module, "get_sql_connection", lambda _key: FakeConnection())

    result = sql.execute_create(
        "gp",
        "mart.target",
        "CREATE TEMP TABLE scratch.source AS SELECT 1; SELECT * FROM scratch.source",
        if_not_exists=True,
        retry_cnt=1,
        timeout_increment=0,
    )

    assert result == 0
    assert events == ["exists", "close"]


def test_create_sql_table_is_deprecated_compatible_alias() -> None:
    with pytest.warns(DeprecationWarning, match="use sql.create_table"):
        plan = sql.create_sql_table(
            "gp",
            "mart.target",
            table_schema={"id": "BIGINT"},
            dry_run=True,
        )

    assert plan.operation == "create_table"


def test_create_table_warns_for_legacy_drop_target_option() -> None:
    with pytest.warns(DeprecationWarning, match="use drop_if_exists"):
        plan = sql.create_table(
            "gp",
            "mart.target",
            table_schema={"id": "BIGINT"},
            drop_target_if_exists=True,
            dry_run=True,
        )

    assert plan.options["drop_if_exists"] is True


def test_insert_returns_rows_without_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        query_writes_module,
        "_execute_sql_options",
        lambda _options: SimpleNamespace(rowcount=3),
    )

    assert sql.insert("gp", "mart.target", "SELECT 1") == 3


def test_query_write_input_validation_and_combined_plan_split() -> None:
    with pytest.raises(TypeError, match="query must be a string"):
        sql.insert("gp", "mart.target", 1)  # type: ignore[arg-type]
    with pytest.raises(InvalidSqlInputError, match="must not be empty"):
        sql.insert("gp", "mart.target", "  ")
    with pytest.raises(TypeError, match="table_name must be a string"):
        sql.insert("gp", 1, "SELECT 1")  # type: ignore[arg-type]
    with pytest.raises(InvalidSqlInputError, match="table_name must not be empty"):
        sql.insert("gp", " ", "SELECT 1")
    with pytest.raises(InvalidSqlInputError, match="valid table identifier"):
        sql.insert("gp", "mart.bad table", "SELECT 1")

    execute_plan = SqlPlan(
        operation="execute",
        target_alias="gp",
        target_backend="gp",
    )
    execute_plan.statements = [SqlStatement("SET x = 1; INSERT INTO mart.target SELECT 1")]
    plan = query_writes_module._build_query_insert_plan(
        operation="execute_insert",
        table_name="mart.target",
        setup_count=1,
        execute_plan=execute_plan,
    )

    assert [statement.phase for statement in plan.statements] == ["setup", "insert_target"]


def test_execute_create_runs_setup_drop_and_create_on_one_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _ExecuteCreateAdapter()
    connection = _ExecuteCreateConnection()
    options = _execute_create_options(
        setup_sqls=["CREATE TEMP TABLE scratch.source AS SELECT 1"],
        drop_sqls=["DROP TABLE IF EXISTS mart.target"],
        if_not_exists=True,
        return_metadata=True,
    )
    plan = execute_create_module._build_execute_create_plan(options)
    monkeypatch.setattr(execute_create_module, "get_backend_adapter", lambda _backend: adapter)
    monkeypatch.setattr(execute_create_module, "get_sql_connection", lambda _key: connection)

    result = execute_create_module._execute_create_options(options, plan)

    assert result.rows == 7
    assert result.affected_rows == 7
    assert adapter.events == [
        "exists",
        ("setup", "CREATE TEMP TABLE scratch.source AS SELECT 1"),
        ("commands", ["DROP TABLE IF EXISTS mart.target"]),
        ("command", "CREATE TABLE mart.target AS SELECT 1 AS id"),
        "after_create",
    ]
    assert connection.closed == 1


def test_execute_create_runs_multi_create_then_one_insert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _ExecuteCreateAdapter()
    connection = _ExecuteCreateConnection()
    options = _execute_create_options(
        backend="ch",
        connection_key="ch",
        create_sqls=[
            "CREATE TABLE mart.target_shard EMPTY AS SELECT 1",
            "CREATE TABLE mart.target",
        ],
        insert_after_create=True,
    )
    plan = execute_create_module._build_execute_create_plan(options)
    monkeypatch.setattr(execute_create_module, "get_backend_adapter", lambda _backend: adapter)
    monkeypatch.setattr(execute_create_module, "get_sql_connection", lambda _key: connection)

    result = execute_create_module._execute_create_options(options, plan)

    assert result == 7
    assert adapter.events == [
        ("commands", []),
        (
            "commands",
            ["CREATE TABLE mart.target_shard EMPTY AS SELECT 1", "CREATE TABLE mart.target"],
        ),
        "after_create",
        ("command", "INSERT INTO mart.target\nSELECT 1 AS id"),
    ]


@pytest.mark.parametrize(
    ("retry_policy", "non_retryable", "expected_error"),
    [
        ("safe", False, sql.AmbiguousSqlMutationError),
        ("always", False, OSError),
        ("safe", True, OSError),
    ],
)
def test_execute_create_error_safety(
    monkeypatch: pytest.MonkeyPatch,
    retry_policy: str,
    non_retryable: bool,
    expected_error: type[Exception],
) -> None:
    adapter = _ExecuteCreateAdapter(failure=OSError("database failure"))
    connection = _ExecuteCreateConnection()
    options = _execute_create_options(retry_policy=retry_policy)
    plan = execute_create_module._build_execute_create_plan(options)
    monkeypatch.setattr(execute_create_module, "get_backend_adapter", lambda _backend: adapter)
    monkeypatch.setattr(execute_create_module, "get_sql_connection", lambda _key: connection)
    monkeypatch.setattr(
        execute_create_module,
        "is_non_retryable_sql_error",
        lambda _error: non_retryable,
    )

    with pytest.raises(expected_error):
        execute_create_module._execute_create_options(options, plan)

    assert connection.closed == 1
    assert connection.rolled_back == int(retry_policy == "always" or non_retryable)


def test_execute_create_connection_and_transactional_presubmission_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = _execute_create_options()
    plan = execute_create_module._build_execute_create_plan(options)
    adapter = _ExecuteCreateAdapter(failure=OSError("before submission"))
    adapter.supports_transactions = True
    connection = _ExecuteCreateConnection()
    monkeypatch.setattr(execute_create_module, "get_backend_adapter", lambda _backend: adapter)
    monkeypatch.setattr(execute_create_module, "get_sql_connection", lambda _key: connection)

    with pytest.raises(OSError, match="before submission"):
        execute_create_module._execute_create_options(options, plan)
    assert connection.rolled_back == 1

    monkeypatch.setattr(
        execute_create_module,
        "get_sql_connection",
        lambda _key: (_ for _ in ()).throw(ValueError("open failed")),
    )
    with pytest.raises(ValueError, match="open failed"):
        execute_create_module._execute_create_options(options, plan)


def test_execute_create_gp_partition_schema_inference(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = _ExecuteCreateAdapter()
    adapter.requires_execute_create_schema_inference = True
    connection = _ExecuteCreateConnection()
    options = _execute_create_options(gp_partitions=SimpleNamespace(start=1, end=2, interval=1))
    plan = execute_create_module._build_execute_create_plan(options)
    monkeypatch.setattr(execute_create_module, "get_backend_adapter", lambda _backend: adapter)
    monkeypatch.setattr(execute_create_module, "get_sql_connection", lambda _key: connection)
    monkeypatch.setattr(
        execute_create_module,
        "_build_gp_partition_create_sqls",
        lambda _options, _connection: ["CREATE TABLE mart.target (id BIGINT) PARTITION BY RANGE"],
    )

    assert execute_create_module._execute_create_options(options, plan) == 7
    assert ("command", "CREATE TABLE mart.target (id BIGINT) PARTITION BY RANGE") in adapter.events


def test_execute_create_helpers_cover_schema_context_and_defensive_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = _execute_create_options(gp_distributed_by_key=["id"], gp_partitions={"start": 1})
    monkeypatch.setattr(
        execute_create_module,
        "inspect_source_query_schema",
        lambda *_args, **_kwargs: [SimpleNamespace(name="id")],
    )
    monkeypatch.setattr(
        execute_create_module,
        "map_source_schema_to_target",
        lambda *_args, **_kwargs: {"id": "BIGINT"},
    )
    monkeypatch.setattr(
        execute_create_module,
        "_build_create_table_sqls",
        lambda *_args, **_kwargs: ["CREATE TABLE mart.target (id BIGINT)"],
    )

    assert execute_create_module._build_gp_partition_create_sqls(options, object()) == [
        "CREATE TABLE mart.target (id BIGINT)"
    ]
    assert execute_create_module._execute_create_context(options, 2).retry_attempt == 2
    with pytest.raises(TypeError, match="flag must be a boolean"):
        execute_create_module._validate_bool(1, "flag")
    execute_create_module._close_connection_quietly(None, options)
    broken = _ExecuteCreateConnection(close_error=True)
    execute_create_module._close_connection_quietly(broken, options)
    assert broken.closed == 1


def test_backend_execute_create_builders_cover_optional_modes() -> None:
    gp_plan = sql.execute_create(
        "gp",
        "mart.target",
        "SELECT id FROM source",
        gp_distributed_by_key="id",
        dry_run=True,
    )
    partition_plan = sql.execute_create(
        "gp",
        "mart.target",
        "SELECT id FROM source",
        gp_partitions={"start": "2025-01-01", "end": "2025-01-03", "interval": "1 day"},
        partition_by="id",
        dry_run=True,
    )
    assert gp_plan.sqls[0].endswith('DISTRIBUTED BY ("id")')
    assert "schema inferred" in partition_plan.sqls[0]

    pair_policy = ClickHouseCreationPolicy(
        create_distributed_pair=True,
        shard_engine="MergeTree",
        shard_on_cluster=None,
        distributed_engine_template=(
            "Distributed({cluster}, {database}, {shard_table}, {sharding_key})"
        ),
        distributed_cluster="analytics",
        distributed_on_cluster=None,
        sharding_key="rand()",
        ddl_ready_timeout_seconds=1,
    )
    single_policy = replace(pair_policy, create_distributed_pair=False)
    pair_commands, needs_insert = build_policy_create_as_sqls(
        table_name="mart.target",
        source_sql="SELECT 1",
        partition_by=None,
        order_by=None,
        policy=pair_policy,
        ch_only_shard=False,
        if_not_exists=False,
    )
    single_commands, single_insert = build_policy_create_as_sqls(
        table_name="mart.target",
        source_sql="SELECT 1",
        partition_by=None,
        order_by=None,
        policy=single_policy,
        ch_only_shard=False,
        if_not_exists=True,
    )
    assert len(pair_commands) == 2
    assert needs_insert is True
    assert single_commands[0].startswith("CREATE TABLE IF NOT EXISTS")
    assert single_insert is False


def test_create_table_mode_validation_existing_shortcuts_and_lazy_exports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(TypeError, match="drop_if_exists"):
        ddl_api_module._resolve_drop_if_exists(
            drop_if_exists=1,
            drop_target_if_exists=None,
        )
    with pytest.warns(DeprecationWarning), pytest.raises(TypeError, match="drop_target"):
        ddl_api_module._resolve_drop_if_exists(
            drop_if_exists=False,
            drop_target_if_exists=1,
        )
    with pytest.warns(DeprecationWarning), pytest.raises(InvalidSqlInputError, match="only"):
        ddl_api_module._resolve_drop_if_exists(
            drop_if_exists=True,
            drop_target_if_exists=True,
        )
    with pytest.raises(TypeError, match="if_not_exists"):
        ddl_api_module._validate_create_mode(
            drop_if_exists=False,
            if_not_exists=1,
            ch_replace_table=False,
        )
    with pytest.raises(InvalidSqlInputError, match="drop_if_exists"):
        ddl_api_module._validate_create_mode(
            drop_if_exists=True,
            if_not_exists=True,
            ch_replace_table=False,
        )
    with pytest.raises(InvalidSqlInputError, match="ch_replace_table"):
        ddl_api_module._validate_create_mode(
            drop_if_exists=False,
            if_not_exists=True,
            ch_replace_table=True,
        )

    assert (
        ddl_api_module._handle_existing_create_target(
            config=SimpleNamespace(connection_key="gp", backend="gp"),
            table_name="mart.target",
            drop_if_exists=True,
            if_not_exists=False,
            ch_replace_table=False,
            query_label=None,
            return_metadata=False,
        )
        is ddl_api_module._CREATE_CONTINUE
    )

    class Connection:
        def __init__(self) -> None:
            self.closed = 0

        def close(self) -> None:
            self.closed += 1

    connection = Connection()
    basic_ops = importlib.import_module("analytics_toolkit.sql.dml.table._basic_ops")
    monkeypatch.setattr(ddl_api_module, "get_sql_connection", lambda _key: connection)
    monkeypatch.setattr(basic_ops, "table_exists", lambda *_args: False)
    kwargs = {
        "config": SimpleNamespace(connection_key="gp", backend="gp"),
        "table_name": "mart.target",
        "drop_if_exists": False,
        "if_not_exists": True,
        "ch_replace_table": False,
        "query_label": "label",
        "return_metadata": False,
    }
    assert (
        ddl_api_module._handle_existing_create_target(**kwargs) is ddl_api_module._CREATE_CONTINUE
    )
    monkeypatch.setattr(basic_ops, "table_exists", lambda *_args: True)
    assert ddl_api_module._handle_existing_create_target(**kwargs) is None
    metadata_result = ddl_api_module._handle_existing_create_target(
        **{**kwargs, "return_metadata": True}
    )
    assert metadata_result.metadata.statement_count == 0
    assert connection.closed == 3

    assert ddl_api_module.__getattr__("create_sql_table") is sql.create_sql_table
    assert ddl_module.__getattr__("create_table") is sql.create_table
    with pytest.raises(AttributeError):
        ddl_api_module.__getattr__("missing")
    with pytest.raises(AttributeError):
        ddl_module.__getattr__("missing")


def test_create_table_returns_if_not_exists_short_circuit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        ddl_api_module,
        "_handle_existing_create_target",
        lambda **_kwargs: None,
    )

    assert (
        sql.create_table(
            "gp",
            "mart.target",
            table_schema={"id": "BIGINT"},
            if_not_exists=True,
        )
        is None
    )
