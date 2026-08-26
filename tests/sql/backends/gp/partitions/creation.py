from __future__ import annotations

from tests.sql._support.partitions import (
    FakeDbapiConnection,
    dml_module,
    dml_table_module,
    pytest,
    sql_module,
    table_ops_module,
)


def test_gp_create_partitions_executes_in_order_and_commits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeDbapiConnection()
    monkeypatch.setattr(
        table_ops_module,
        "get_sql_connection",
        lambda key: connection,
    )

    result = table_ops_module.gp_create_partitions(
        "gp",
        "sandbox.events",
        days=["2026-05-01", "2026-05-02"],
        retry_cnt=1,
        timeout_increment=0,
        return_metadata=True,
    )

    assert connection.executed == [
        "ALTER TABLE sandbox.events ADD PARTITION p_2026_05_01 "
        "START ('2026-05-01') INCLUSIVE END ('2026-05-02') EXCLUSIVE",
        "ALTER TABLE sandbox.events ADD PARTITION p_2026_05_02 "
        "START ('2026-05-02') INCLUSIVE END ('2026-05-03') EXCLUSIVE",
    ]
    assert connection.commit_calls == 1
    assert connection.rollback_calls == 0
    assert connection.close_calls == 1
    assert result.rows is None
    assert result.metadata.statement_count == 2
    assert result.metadata.retry_attempts == 1
    assert result.metadata.operation_status == "success"
    assert result.plan.operation == "gp_create_partitions"


def test_gp_create_partitions_is_public_and_timed() -> None:
    assert sql_module.gp_create_partitions is table_ops_module.gp_create_partitions
    assert dml_module.gp_create_partitions is table_ops_module.gp_create_partitions
    assert dml_table_module.gp_create_partitions is table_ops_module.gp_create_partitions
    assert "gp_create_partitions" in sql_module.__all__
    assert "gp_create_partitions" in dml_module.__all__
    assert "gp_create_partitions" in dml_table_module.__all__
    assert "gp_create_partitions" in sql_module._TIMED_PUBLIC_SQL_FUNCTION_NAMES
    assert getattr(sql_module.gp_create_partitions, "__sql_public_timing__", False)
    assert "gp_create_many_partitions" not in sql_module.__all__
    assert "gp_create_many_partitions" not in dml_module.__all__
    assert "gp_create_many_partitions" not in dml_table_module.__all__
    assert "gp_create_many_partitions" not in sql_module._TIMED_PUBLIC_SQL_FUNCTION_NAMES
    assert not hasattr(sql_module, "gp_create_many_partitions")
    assert "build_gp_create_partitions_sqls" not in sql_module.__all__
    assert not hasattr(sql_module, "build_gp_create_partitions_sqls")


def test_gp_create_partitions_only_generate_sql_applies_query_label() -> None:
    generated_sql = table_ops_module.gp_create_partitions(
        "gp",
        "sandbox.events",
        days=["2026-05-01"],
        query_label="create daily partitions",
        only_generate_sql=True,
    )

    assert generated_sql == (
        "/* analytics_toolkit query_label=create daily partitions */\n"
        "ALTER TABLE sandbox.events ADD PARTITION p_2026_05_01 "
        "START ('2026-05-01') INCLUSIVE END ('2026-05-02') EXCLUSIVE"
    )


def test_gp_create_partitions_only_generate_sql_renders_intervals_and_values() -> None:
    assert table_ops_module.gp_create_partitions(
        "gp",
        "sandbox.events",
        intervals=[
            {"start": "2026-05-01", "end": "2026-05-02"},
            {
                "name": "custom_p_20260502",
                "start": "2026-05-02",
                "end": "2026-05-03",
            },
        ],
        name_template="part_{}",
        only_generate_sql=True,
    ) == (
        "ALTER TABLE sandbox.events ADD PARTITION part_2026_05_01 "
        "START ('2026-05-01') INCLUSIVE END ('2026-05-02') EXCLUSIVE;\n"
        "ALTER TABLE sandbox.events ADD PARTITION custom_p_20260502 "
        "START ('2026-05-02') INCLUSIVE END ('2026-05-03') EXCLUSIVE"
    )
    assert table_ops_module.gp_create_partitions(
        "gp",
        "sandbox.events_by_country",
        values=["RU", "Cote d'Ivoire"],
        only_generate_sql=True,
    ) == (
        "ALTER TABLE sandbox.events_by_country ADD PARTITION p_RU VALUES ('RU');\n"
        "ALTER TABLE sandbox.events_by_country ADD PARTITION p_Cote_d_Ivoire "
        "VALUES ('Cote d''Ivoire')"
    )


def test_gp_create_partitions_only_generate_sql_renders_period_ranges() -> None:
    assert table_ops_module.gp_create_partitions(
        "gp",
        "sandbox.events",
        days=["2026-05-01", "2026-05-02"],
        only_generate_sql=True,
    ) == (
        "ALTER TABLE sandbox.events ADD PARTITION p_2026_05_01 "
        "START ('2026-05-01') INCLUSIVE END ('2026-05-02') EXCLUSIVE;\n"
        "ALTER TABLE sandbox.events ADD PARTITION p_2026_05_02 "
        "START ('2026-05-02') INCLUSIVE END ('2026-05-03') EXCLUSIVE"
    )
    assert table_ops_module.gp_create_partitions(
        "gp",
        "sandbox.events",
        weeks=["2026-05-04"],
        only_generate_sql=True,
    ) == (
        "ALTER TABLE sandbox.events ADD PARTITION p_2026_05_04 "
        "START ('2026-05-04') INCLUSIVE END ('2026-05-11') EXCLUSIVE"
    )
    assert table_ops_module.gp_create_partitions(
        "gp",
        "sandbox.events",
        months=["2026-12-01"],
        only_generate_sql=True,
    ) == (
        "ALTER TABLE sandbox.events ADD PARTITION p_2026_12_01 "
        "START ('2026-12-01') INCLUSIVE END ('2027-01-01') EXCLUSIVE"
    )
    assert table_ops_module.gp_create_partitions(
        "gp",
        "sandbox.events",
        years=["2026-01-01"],
        only_generate_sql=True,
    ) == (
        "ALTER TABLE sandbox.events ADD PARTITION p_2026_01_01 "
        "START ('2026-01-01') INCLUSIVE END ('2027-01-01') EXCLUSIVE"
    )
