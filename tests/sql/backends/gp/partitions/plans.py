from __future__ import annotations

from tests.sql._support.partitions import (
    Any,
    _stub_leaf_partition_discovery,
    gp_maintenance_module,
    importlib,
    pd,
    pytest,
    table_ops_module,
)


def test_gp_analyze_partitioned_table_builds_labeled_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_leaf_partition_discovery(
        monkeypatch,
        ["reporting.events_1_prt_2026_01", "reporting.events_1_prt_2026_02"],
    )
    plan = gp_maintenance_module.gp_analyze_partitioned_table(
        "gp",
        "reporting.events",
        ["reporting.events_1_prt_2026_01", "reporting.events_1_prt_2026_02"],
        concurrency=2,
        dry_run=True,
        query_label="refresh partition statistics",
    )

    assert plan.operation == "gp_analyze_partitioned_table"
    assert plan.options == {
        "partition_names": [
            '"reporting"."events_1_prt_2026_01"',
            '"reporting"."events_1_prt_2026_02"',
        ],
        "table_names": ['"reporting"."events"'],
        "concurrency": 2,
    }
    assert plan.metadata.statement_count == 2
    assert plan.sqls == [
        "/* analytics_toolkit query_label=refresh partition statistics */\n"
        'ANALYZE "reporting"."events_1_prt_2026_01"',
        "/* analytics_toolkit query_label=refresh partition statistics */\n"
        'ANALYZE "reporting"."events_1_prt_2026_02"',
    ]


def test_gp_analyze_partitioned_table_discovers_leaf_partitions_for_dry_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def discover(_db_key: str, query: str, **_kwargs: Any) -> pd.DataFrame:
        calls.append(query)
        return pd.DataFrame(
            [
                {"schema_name": "analytics", "relation_name": "orders_1_prt_b"},
                {"schema_name": "analytics", "relation_name": "orders_1_prt_a"},
            ]
        )

    read_sql_module = importlib.import_module("analytics_toolkit.sql.dml.io.read_sql")
    monkeypatch.setattr(read_sql_module, "read_sql", discover)

    plan = gp_maintenance_module.gp_analyze_partitioned_table(
        "gp", "analytics.orders", dry_run=True
    )

    assert len(calls) == 1
    assert "FROM pg_catalog.pg_inherits" in calls[0]
    assert plan.options["table_names"] == ['"analytics"."orders"']
    assert plan.options["partition_names"] == [
        '"analytics"."orders_1_prt_b"',
        '"analytics"."orders_1_prt_a"',
    ]


def test_gp_create_partitions_dry_run_returns_plan_without_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        table_ops_module,
        "get_sql_connection",
        lambda key: pytest.fail("connection should not be opened"),
    )

    plan = table_ops_module.gp_create_partitions(
        "gp_sandbox",
        "sandbox.events",
        days=["2026-05-01", "2026-05-02"],
        dry_run=True,
        query_label="create partitions",
    )

    assert plan.operation == "gp_create_partitions"
    assert plan.target_alias == "gp_sandbox"
    assert plan.target_backend == "gp"
    assert plan.target_table == "sandbox.events"
    assert plan.options == {
        "partition_input": "days",
        "name_template": "p_{}",
    }
    assert plan.metadata.statement_count == 2
    assert plan.metadata.query_label == "create partitions"
    assert [statement.phase for statement in plan.statements] == [
        "create_partitions",
        "create_partitions",
    ]
    assert plan.sqls[0].startswith("/* analytics_toolkit query_label=create partitions */")


def test_gp_create_partitions_return_sql_returns_plan_without_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        table_ops_module,
        "get_sql_connection",
        lambda key: pytest.fail("connection should not be opened"),
    )

    plan = table_ops_module.gp_create_partitions(
        "gp",
        "sandbox.events",
        values=["RU"],
        return_sql=True,
    )

    assert plan.operation == "gp_create_partitions"
    assert plan.sqls == ["ALTER TABLE sandbox.events ADD PARTITION p_RU VALUES ('RU')"]
