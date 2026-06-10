from __future__ import annotations

import importlib
from typing import Any

import pytest

from analytics_toolkit.sql.connection.errors import (
    InvalidSqlInputError,
    UnsupportedConnectionTypeError,
)
from tests.sql_fakes import FakeDbapiConnection, FakeDbapiCursor


sql_module = importlib.import_module("analytics_toolkit.sql")
dml_module = importlib.import_module("analytics_toolkit.sql.dml")
dml_table_module = importlib.import_module("analytics_toolkit.sql.dml.table")
table_ops_module = importlib.import_module("analytics_toolkit.sql.dml.table.partitions")


def test_gp_create_partitions_is_public_and_timed() -> None:
    assert (
        sql_module.gp_create_partitions
        is table_ops_module.gp_create_partitions
    )
    assert (
        dml_module.gp_create_partitions
        is table_ops_module.gp_create_partitions
    )
    assert (
        dml_table_module.gp_create_partitions
        is table_ops_module.gp_create_partitions
    )
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
    assert plan.sqls[0].startswith(
        "/* analytics_toolkit query_label=create partitions */"
    )


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
    assert plan.sqls == [
        "ALTER TABLE sandbox.events ADD PARTITION p_RU VALUES ('RU')"
    ]


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


def test_gp_create_partitions_retries_with_fresh_connection_and_rolls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_connection = _FailingOnceGpConnection()
    second_connection = _FailingOnceGpConnection(fail_first_execute=False)
    connections = [first_connection, second_connection]

    monkeypatch.setattr(
        table_ops_module,
        "get_sql_connection",
        lambda key: connections.pop(0),
    )

    result = table_ops_module.gp_create_partitions(
        "gp",
        "sandbox.events",
        days=["2026-05-01", "2026-05-02"],
        retry_cnt=2,
        timeout_increment=0,
        return_metadata=True,
    )

    assert first_connection.executed == [
        "ALTER TABLE sandbox.events ADD PARTITION p_2026_05_01 "
        "START ('2026-05-01') INCLUSIVE END ('2026-05-02') EXCLUSIVE"
    ]
    assert first_connection.rollback_calls >= 1
    assert first_connection.commit_calls == 0
    assert first_connection.close_calls == 1
    assert second_connection.executed == [
        "ALTER TABLE sandbox.events ADD PARTITION p_2026_05_01 "
        "START ('2026-05-01') INCLUSIVE END ('2026-05-02') EXCLUSIVE",
        "ALTER TABLE sandbox.events ADD PARTITION p_2026_05_02 "
        "START ('2026-05-02') INCLUSIVE END ('2026-05-03') EXCLUSIVE",
    ]
    assert second_connection.rollback_calls == 0
    assert second_connection.commit_calls == 1
    assert second_connection.close_calls == 1
    assert result.metadata.retry_attempts == 2
    assert result.metadata.operation_status == "success"


def test_gp_create_partitions_rejects_non_gp_alias() -> None:
    with pytest.raises(UnsupportedConnectionTypeError, match="requires a gp"):
        table_ops_module.gp_create_partitions(
            "trino",
            "sandbox.events",
            days=["2026-05-01"],
            dry_run=True,
        )


def test_gp_create_partitions_validates_exactly_one_input() -> None:
    with pytest.raises(InvalidSqlInputError, match="Exactly one"):
        table_ops_module.gp_create_partitions(
            "gp",
            "sandbox.events",
            only_generate_sql=True,
        )

    with pytest.raises(InvalidSqlInputError, match="Exactly one"):
        table_ops_module.gp_create_partitions(
            "gp",
            "sandbox.events",
            days=["2026-05-01"],
            months=["2026-05-01"],
            only_generate_sql=True,
        )

    with pytest.raises(InvalidSqlInputError, match="non-empty sequence"):
        table_ops_module.gp_create_partitions(
            "gp",
            "sandbox.events",
            days=[],
            only_generate_sql=True,
        )


@pytest.mark.parametrize(
    ("argument", "values", "match"),
    [
        ("weeks", ["2026-05-05"], "Monday"),
        ("months", ["2026-05-02"], "month starts"),
        ("years", ["2026-02-01"], "year starts"),
    ],
)
def test_gp_create_partitions_validates_period_starts(
    argument: str,
    values: list[str],
    match: str,
) -> None:
    with pytest.raises(InvalidSqlInputError, match=match):
        table_ops_module.gp_create_partitions(
            "gp",
            "sandbox.events",
            only_generate_sql=True,
            **{argument: values},
        )


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"days": ["not-a-date"]}, "valid ISO date"),
        ({"values": [" "]}, "empty strings"),
        (
            {"intervals": [{"start": "2026-05-02", "end": "2026-05-01"}]},
            "after interval start",
        ),
        ({"intervals": [{"start": "2026-05-01"}]}, "ISO date"),
        ({"days": ["2026-05-01"], "name_template": "p"},
            "name_template"),
        ({"days": ["2026-05-01"], "name_template": "p_{}_{}"},
            "name_template"),
        ({"days": ["2026-05-01"], "name_template": "{}"},
            "unquoted SQL identifier"),
        (
            {
                "intervals": [
                    {
                        "name": "bad-name",
                        "start": "2026-05-01",
                        "end": "2026-05-02",
                    }
                ]
            },
            "unquoted SQL identifier",
        ),
        ({"days": ["2026-05-01"], "table": " "}, "Table name"),
    ],
)
def test_gp_create_partitions_validates_invalid_inputs(
    kwargs: dict[str, Any],
    match: str,
) -> None:
    table = kwargs.pop("table", "sandbox.events")

    with pytest.raises(InvalidSqlInputError, match=match):
        table_ops_module.gp_create_partitions(
            "gp",
            table,
            only_generate_sql=True,
            **kwargs,
        )


class _FailingOnceGpCursor(FakeDbapiCursor):
    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> None:
        super().execute(sql, params)
        if self.connection.fail_first_execute:
            self.connection.fail_first_execute = False
            raise RuntimeError("temporary failure")


class _FailingOnceGpConnection(FakeDbapiConnection):
    def __init__(self, fail_first_execute: bool = True) -> None:
        super().__init__()
        self.fail_first_execute = fail_first_execute

    def cursor(self) -> _FailingOnceGpCursor:
        return _FailingOnceGpCursor(self)
