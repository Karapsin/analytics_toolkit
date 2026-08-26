from __future__ import annotations

from tests.sql._support.partitions import (
    Any,
    FakeDbapiConnection,
    InvalidSqlInputError,
    _stub_leaf_partition_discovery,
    date,
    datetime,
    dml_module,
    dml_table_module,
    gp_maintenance_module,
    pytest,
    sql_module,
    table_ops_module,
    timezone,
)


def test_gp_analyze_partitioned_table_executes_each_partition_with_fresh_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_leaf_partition_discovery(
        monkeypatch,
        ["analytics.orders_1_prt_a", "analytics.orders_1_prt_b"],
    )
    first_connection = FakeDbapiConnection()
    second_connection = FakeDbapiConnection()
    connections = [first_connection, second_connection]
    monkeypatch.setattr(
        gp_maintenance_module, "get_sql_connection", lambda _key: connections.pop(0)
    )

    result = gp_maintenance_module.gp_analyze_partitioned_table(
        "gp",
        "analytics.orders",
        ["analytics.orders_1_prt_a", "analytics.orders_1_prt_b"],
        retry_cnt=1,
        timeout_increment=0,
        return_metadata=True,
    )

    assert first_connection.executed == ['ANALYZE "analytics"."orders_1_prt_a"']
    assert second_connection.executed == ['ANALYZE "analytics"."orders_1_prt_b"']
    assert first_connection.close_calls == second_connection.close_calls == 1
    assert result.metadata.operation_status == "success"
    assert result.metadata.retry_attempts == 1


def test_gp_analyze_partitioned_table_is_public_and_timed() -> None:
    assert (
        sql_module.gp_analyze_partitioned_table
        is gp_maintenance_module.gp_analyze_partitioned_table
    )
    assert "gp_analyze_partitioned_table" in sql_module.__all__
    assert "gp_analyze_partitioned_table" in dml_module.__all__
    assert "gp_analyze_partitioned_table" in dml_table_module.__all__
    assert "gp_analyze_partitioned_table" in sql_module._TIMED_PUBLIC_SQL_FUNCTION_NAMES
    assert getattr(sql_module.gp_analyze_partitioned_table, "__sql_public_timing__", False)


def test_gp_partition_normalizers_cover_remaining_type_and_date_guards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        table_ops_module,
        "_selected_gp_create_partition_input",
        lambda **_kwargs: "unexpected",
    )
    with pytest.raises(AssertionError, match="Unexpected"):
        table_ops_module._normalize_gp_create_partitions(
            intervals=None,
            values=None,
            days=None,
            weeks=None,
            months=None,
            years=None,
            name_template="p_{}",
        )

    with pytest.raises(InvalidSqlInputError, match="contain mappings"):
        table_ops_module._normalize_gp_interval_partitions(["bad"], "p_{}")
    with pytest.raises(InvalidSqlInputError, match="contain strings"):
        table_ops_module._normalize_gp_value_partitions([1], "p_{}")

    for value in (None, "value", {"bad": "mapping"}, 1):
        with pytest.raises(InvalidSqlInputError, match=r"non-empty sequence|provided"):
            table_ops_module._validate_gp_partition_sequence(value, "values")

    assert table_ops_module._parse_gp_partition_date(
        datetime(2026, 5, 1, 12, tzinfo=timezone.utc),
        "date",
    ) == date(2026, 5, 1)
    assert table_ops_module._parse_gp_partition_date(
        date(2026, 5, 2),
        "date",
    ) == date(2026, 5, 2)
    with pytest.raises(InvalidSqlInputError, match="ISO date"):
        table_ops_module._parse_gp_partition_date(" ", "date")
    with pytest.raises(AssertionError, match="Unexpected"):
        table_ops_module._next_gp_partition_period_start(date(2026, 1, 1), "quarters")
    with pytest.raises(InvalidSqlInputError, match="must be a string"):
        table_ops_module._validate_gp_partition_name_template(1)
    with pytest.raises(InvalidSqlInputError, match="must be a string"):
        table_ops_module._validate_gp_partition_identifier(1, "partition")
    with pytest.raises(InvalidSqlInputError, match="must not be empty"):
        table_ops_module._validate_gp_partition_identifier(" ", "partition")

    with pytest.raises(InvalidSqlInputError, match="must be strings"):
        table_ops_module._validate_partition_keys([1])
    with pytest.raises(InvalidSqlInputError, match="must not be empty"):
        table_ops_module._validate_partition_keys([" "])


def test_partition_lifecycle_metadata_results_and_contexts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contexts: list[object] = []

    def fake_run_connection_operation(**kwargs: Any) -> None:
        contexts.append(kwargs["context_factory"](1))

    monkeypatch.setattr(
        table_ops_module,
        "run_connection_operation",
        fake_run_connection_operation,
    )
    created = table_ops_module.gp_create_partitions(
        "gp",
        "sandbox.events",
        days=["2026-05-01"],
        return_metadata=True,
    )
    dropped = table_ops_module.drop_partitions(
        "gp",
        "sandbox.events",
        ["2026-05-01"],
        return_metadata=True,
    )
    assert created.metadata.statement_count == 1
    assert dropped.metadata.statement_count == 1
    assert [context.operation for context in contexts] == [
        "gp_create_partitions",
        "drop_partitions",
    ]
    assert (
        table_ops_module.gp_create_partitions(
            "gp",
            "sandbox.events",
            days=["2026-05-02"],
        )
        is None
    )
