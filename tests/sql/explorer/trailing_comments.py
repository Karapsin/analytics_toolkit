from __future__ import annotations

import pytest
import sqlglot
from analytics_toolkit._sql_statements import split_statements
from analytics_toolkit.sql_explorer.statements import ExecutionRoute, build_execution_plan


@pytest.mark.parametrize(
    "source",
    [
        "select 1;\n-- comment",
        "select 1; -- comment",
        "select 1\n-- comment",
        "select 1\n/* comment */",
        "select 1; /* comment */",
    ],
)
@pytest.mark.parametrize(
    ("backend", "dialect"), [("gp", "postgres"), ("trino", "trino"), ("ch", "clickhouse")]
)
def test_trailing_comments_remain_one_executable_query(
    source: str, backend: str, dialect: str
) -> None:
    plan = build_execution_plan(source, backend)
    assert plan.statement_count == 1
    assert plan.route == ExecutionRoute.READ
    assert not plan.requires_confirmation
    assert "comment" in plan.execution_sql
    assert len(sqlglot.parse(plan.execution_sql, read=dialect)) == 1
    assert len(split_statements(plan.full_execution_sql)) == 1


def test_comments_and_literals_do_not_create_statements() -> None:
    assert split_statements("-- only\n/* comment */") == []
    assert len(split_statements("select ';--'; -- tail\nselect '/* literal */'; /* tail */")) == 2
    plan = build_execution_plan("select 1; -- tail\nselect 2; -- tail", "gp")
    assert len(split_statements(plan.execution_sql)) == 2


def test_comment_keywords_and_empty_delimiters_do_not_change_routing() -> None:
    for source in (
        "select 1; -- returning value",
        "select 1; /* into table */",
        "select 1;;; -- done",
    ):
        plan = build_execution_plan(source, "gp")
        assert plan.statement_count == 1
        assert not plan.requires_confirmation
        assert sqlglot.parse_one(plan.execution_sql, read="postgres")
    assert not build_execution_plan("select 1 FORMAT JSON; -- comment", "ch").server_limited
