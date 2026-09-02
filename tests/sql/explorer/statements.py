from __future__ import annotations

import pytest
from analytics_toolkit.sql_explorer import statements as statements_module
from analytics_toolkit.sql_explorer.errors import SqlExplorerConfigurationError
from analytics_toolkit.sql_explorer.statements import (
    ExecutionRoute,
    build_execution_plan,
)


def test_single_query_uses_read_and_fetches_one_extra_row() -> None:
    plan = build_execution_plan("select value from metrics;", "gp")

    assert plan.statements == ("select value from metrics",)
    assert plan.route is ExecutionRoute.READ
    assert plan.returns_rows is True
    assert plan.requires_confirmation is False
    assert plan.server_limited is True
    assert "LIMIT 201" in plan.execution_sql


def test_result_wrapper_preserves_editor_line_numbers() -> None:
    plan = build_execution_plan("select 1\nas select 1", "gp")

    assert plan.execution_sql.splitlines()[1].startswith("as select 1")


def test_setup_and_final_query_use_execute_read_without_double_semicolon() -> None:
    plan = build_execution_plan(
        "create temp table sample(value int); select value from sample;",
        "gp",
    )

    assert plan.route is ExecutionRoute.EXECUTE_READ
    assert plan.statement_count == 2
    assert plan.requires_confirmation is True
    assert ";;" not in plan.execution_sql
    assert plan.execution_sql.startswith("create temp table sample(value int);\n")


@pytest.mark.parametrize(
    ("statement", "returns_rows", "requires_confirmation"),
    [
        ("create table sample(value int)", False, True),
        ("insert into sample values (1) returning value", True, True),
        ("show tables", True, False),
        ("values (1), (2)", True, False),
        ("explain select * from sample", True, False),
        ("explain analyze delete from sample", True, True),
        ("select * into temp copied from sample", False, True),
    ],
)
def test_statement_shape_controls_result_and_confirmation(
    statement: str,
    returns_rows: bool,
    requires_confirmation: bool,
) -> None:
    plan = build_execution_plan(statement, "gp")

    assert plan.returns_rows is returns_rows
    assert plan.requires_confirmation is requires_confirmation
    expected_route = ExecutionRoute.READ if returns_rows else ExecutionRoute.EXECUTE
    assert plan.route is expected_route


def test_clickhouse_format_query_is_not_wrapped() -> None:
    plan = build_execution_plan("select number from numbers(10) FORMAT JSON", "ch")

    assert plan.route is ExecutionRoute.READ
    assert plan.server_limited is False
    assert plan.execution_sql.endswith("FORMAT JSON")


def test_comments_before_query_are_supported() -> None:
    plan = build_execution_plan("-- inspect\nselect 1", "trino")

    assert plan.route is ExecutionRoute.READ
    assert plan.requires_confirmation is False


def test_empty_editor_is_rejected() -> None:
    with pytest.raises(SqlExplorerConfigurationError, match="Enter a SQL statement"):
        build_execution_plan("  ; -- nothing\n", "gp")


def test_parser_fallback_classifies_and_wraps_select(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(statements_module, "_parse_expression", lambda statement, dialect: None)

    plan = build_execution_plan("select value from sample", "gp")

    assert plan.route is ExecutionRoute.READ
    assert plan.requires_confirmation is False
    assert plan.server_limited is True


def test_parser_fallback_handles_returning_and_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(statements_module, "_parse_expression", lambda statement, dialect: None)

    returning = build_execution_plan("update sample set value = 1 returning value", "gp")
    values = build_execution_plan("values (1)", "gp")

    assert returning.returns_rows is True
    assert returning.requires_confirmation is True
    assert values.server_limited is True


def test_parse_error_and_empty_sqlparse_result_use_fallbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_parse(*args: object, **kwargs: object) -> None:
        del args, kwargs
        message = "parse failed"
        raise ValueError(message)

    monkeypatch.setattr(statements_module.sqlglot, "parse_one", fail_parse)

    assert statements_module._parse_expression("broken", "postgres") is None
    assert statements_module._sqlparse_statement_type(()) == ""


def test_nonquery_expression_result_name_is_supported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_expression = type("Describe", (), {"args": {}})()
    monkeypatch.setattr(
        statements_module,
        "_parse_expression",
        lambda statement, dialect: fake_expression,
    )

    assert statements_module._returns_rows("custom command", "postgres") is True
    assert statements_module._is_pure_result_read("custom command", "postgres") is True
