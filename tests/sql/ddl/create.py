from __future__ import annotations

from tests.sql._support.create_from_sql import create_module
from tests.sql._support.cross_area import (
    FakeDbapiConnection,
    ch_ctas_module,
    ddl_create_table_module,
    pd,
    pytest,
)


def test_create_sql_table_logs_generated_sql_preview(monkeypatch, capsys) -> None:
    connection = FakeDbapiConnection()
    monkeypatch.setattr(
        ddl_create_table_module,
        "get_sql_connection",
        lambda _key: connection,
    )

    ddl_create_table_module.create_table(
        db_key="gp",
        table_name="sandbox.created_table",
        df=pd.DataFrame({"id": [1]}),
        retry_cnt=1,
        timeout_increment=0,
    )

    output = capsys.readouterr().out
    assert "[create_table] [gp/gp] [create_target] Finished SQL in " in output
    assert "Finished SQL statement:\nCREATE TABLE sandbox.created_table" in output
    assert connection.executed[0].startswith("CREATE TABLE sandbox.created_table")


def test_create_sql_table_only_generate_sql_accepts_schema_without_dataframe(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        ddl_create_table_module,
        "get_sql_connection",
        lambda _key: pytest.fail("connection should not be opened"),
    )

    ddl = ddl_create_table_module.create_table(
        db_key="gp",
        table_name="sandbox.schema_only",
        table_schema={"user_id": "BIGINT", "score": "DOUBLE PRECISION"},
        only_generate_sql=True,
    )

    assert "CREATE TABLE sandbox.schema_only" in ddl
    assert '"user_id" BIGINT' in ddl
    assert '"score" DOUBLE PRECISION' in ddl


def test_create_sql_table_accepts_schema_without_dataframe(monkeypatch) -> None:
    connection = FakeDbapiConnection()
    opened_keys: list[str] = []

    def fake_get_sql_connection(db_key: str) -> FakeDbapiConnection:
        opened_keys.append(db_key)
        return connection

    monkeypatch.setattr(
        ddl_create_table_module,
        "get_sql_connection",
        fake_get_sql_connection,
    )

    ddl_create_table_module.create_table(
        db_key="gp_sandbox",
        table_name="sandbox.schema_only",
        table_schema={"user_id": "BIGINT", "score": "DOUBLE PRECISION"},
        retry_cnt=1,
        timeout_increment=0,
    )

    assert opened_keys == ["gp_sandbox"]
    assert connection.executed[0].startswith("CREATE TABLE sandbox.schema_only")
    assert '"user_id" BIGINT' in connection.executed[0]
    assert connection.close_calls == 1


def test_create_sql_table_dry_run_and_return_sql_do_not_open_connection(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        ddl_create_table_module,
        "get_sql_connection",
        lambda _key: pytest.fail("connection should not be opened"),
    )

    dry_run_plan = ddl_create_table_module.create_table(
        db_key="gp",
        table_name="sandbox.schema_only",
        table_schema={"id": "BIGINT"},
        dry_run=True,
    )
    return_sql_plan = ddl_create_table_module.create_table(
        db_key="gp",
        table_name="sandbox.schema_only",
        table_schema={"id": "BIGINT"},
        return_sql=True,
    )

    assert dry_run_plan.sqls == return_sql_plan.sqls
    assert dry_run_plan.sqls[0].startswith("CREATE TABLE sandbox.schema_only")


def test_create_sql_table_from_sql_clickhouse_dry_run_uses_shared_plan_steps(monkeypatch) -> None:
    for module in (ddl_create_table_module, create_module):
        monkeypatch.setattr(
            module, "get_sql_connection", lambda *_args: pytest.fail("plan opened a connection")
        )
    plan = ddl_create_table_module.create_table(
        "ch",
        "analytics.events",
        sql="select id from source_table",
        source_db="gp",
        drop_if_exists=True,
        insert_data=True,
        dry_run=True,
        ch_shard_on_cluster="analytics",
        ch_distributed_on_cluster="analytics",
        ch_distributed_cluster="analytics",
    )

    assert [statement.phase for statement in plan.statements] == [
        "inspect_source_schema",
        "create_target",
        "insert_data",
    ]
    assert plan.sqls[1] == "CREATE TABLE analytics.events (<source query schema>)"
    assert plan.options["pending_existing_layout_verification"] is True
    assert plan.options["pending_resource_scope_resolution"] is True
    assert plan.metadata.statement_count is None
    assert not any(statement.startswith("DROP") for statement in plan.sqls)


def test_create_sql_table_from_sql_clickhouse_only_shard_dry_run_uses_local_target(
    monkeypatch,
) -> None:
    for module in (ddl_create_table_module, create_module):
        monkeypatch.setattr(
            module, "get_sql_connection", lambda *_args: pytest.fail("plan opened a connection")
        )
    plan = ddl_create_table_module.create_table(
        "ch",
        "analytics.events",
        sql="select dt, id from source_table",
        source_db="gp",
        drop_if_exists=True,
        insert_data=True,
        dry_run=True,
        ch_only_shard=True,
        partition_by=["dt"],
        order_by=["dt", "id"],
        ch_shard_on_cluster="analytics",
        ch_distributed_on_cluster="analytics",
        ch_distributed_cluster="analytics",
    )

    assert plan.options["ch_only_shard"] is True
    assert [statement.phase for statement in plan.statements] == [
        "inspect_source_schema",
        "create_target",
        "insert_data",
    ]
    assert plan.sqls[1] == "CREATE TABLE analytics.events (<source query schema>)"
    assert plan.options["pending_existing_layout_verification"] is True
    assert plan.options["pending_resource_scope_resolution"] is True
    assert plan.metadata.statement_count is None
    assert not any(statement.startswith("DROP") for statement in plan.sqls)
    assert "_shard" not in "\n".join(plan.sqls)
    assert "ON CLUSTER" not in "\n".join(plan.sqls)
    assert "ENGINE = Distributed(" not in "\n".join(plan.sqls)


def test_ch_create_table_as_dry_run_uses_lifecycle_drop_order() -> None:
    plan = ch_ctas_module.ch_create_table_as(
        "ch",
        "analytics.events",
        "select 1 as id",
        dry_run=True,
        ch_cluster="analytics",
    )

    assert [statement.phase for statement in plan.statements] == [
        "create_target",
        "create_target",
        "create_target",
        "create_target",
        "insert_target",
    ]
    assert plan.options["pending_existing_layout_verification"] is True
    assert plan.options["pending_resource_scope_resolution"] is True
    assert plan.metadata.statement_count is None
    assert all(not statement.lstrip().startswith("DROP ") for statement in plan.sqls)
    assert plan.sqls[0].startswith("CREATE TABLE analytics.events_shard")
    assert plan.sqls[-2].startswith("CREATE TABLE IF NOT EXISTS analytics.events")
    assert "ENGINE = Distributed(" in plan.sqls[-2]
    assert plan.sqls[-1] == "INSERT INTO analytics.events\nselect 1 as id"


def test_ch_create_table_as_only_shard_dry_run_uses_local_target() -> None:
    plan = ch_ctas_module.ch_create_table_as(
        "ch",
        "analytics.events",
        "select 1 as id",
        dry_run=True,
        table_schema={"id": "UInt64"},
        ch_only_shard=True,
        ch_cluster="analytics",
    )

    assert plan.options["ch_only_shard"] is True
    assert [statement.phase for statement in plan.statements] == [
        "create_target",
        "insert_target",
    ]
    assert plan.options["pending_existing_layout_verification"] is True
    assert plan.options["pending_resource_scope_resolution"] is True
    assert plan.metadata.statement_count is None
    assert all(not statement.lstrip().startswith("DROP ") for statement in plan.sqls)
    assert plan.sqls[0].startswith("CREATE TABLE analytics.events")
    assert "ENGINE = ReplicatedMergeTree" in plan.sqls[0]
    assert "_shard" not in "\n".join(plan.sqls)
    assert "ON CLUSTER" not in "\n".join(plan.sqls)
    assert "ENGINE = Distributed(" not in "\n".join(plan.sqls)
