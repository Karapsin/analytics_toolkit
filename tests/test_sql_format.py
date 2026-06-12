from __future__ import annotations

import pytest


def test_sql_format_is_importable_from_root_package() -> None:
    from analytics_toolkit import sql_format

    assert sql_format.format_sql("select 1") == "SELECT\n    1"


def test_format_sql_basic_pretty_output() -> None:
    from analytics_toolkit.sql_format import format_sql

    assert (
        format_sql("select a,b from t order by b desc")
        == "SELECT\n"
        "    a,\n"
        "    b\n"
        "FROM t\n"
        "ORDER BY\n"
        "    b DESC"
    )


def test_format_sql_adds_default_where_anchor() -> None:
    from analytics_toolkit.sql_format import format_sql

    assert (
        format_sql("select * from t where x=1")
        == "SELECT\n"
        "    *\n"
        "FROM t\n"
        "WHERE 1=1\n"
        "      AND x = 1"
    )


def test_format_sql_supports_trailing_and_leading_commas() -> None:
    from analytics_toolkit.sql_format import format_sql

    assert (
        format_sql("select a,b from t", leading_commas=False)
        == "SELECT\n"
        "    a,\n"
        "    b\n"
        "FROM t"
    )
    assert (
        format_sql("select a,b from t", leading_commas=True)
        == "SELECT\n"
        "    a\n"
        "    , b\n"
        "FROM t"
    )


@pytest.mark.parametrize(
    ("where_anchor", "expected_where"),
    [
        ("1=1", "WHERE 1=1\n      AND x = 1"),
        ("true", "WHERE TRUE\n      AND x = 1"),
        ("first_condition", "WHERE\n    x = 1"),
        ("preserve", "WHERE\n    1 = 1 AND x = 1"),
    ],
)
def test_format_sql_where_anchor_modes(
    where_anchor: str,
    expected_where: str,
) -> None:
    from analytics_toolkit.sql_format import format_sql

    assert (
        format_sql(
            "select * from t where 1=1 and x=1",
            where_anchor=where_anchor,
        )
        == "SELECT\n"
        "    *\n"
        "FROM t\n"
        f"{expected_where}"
    )


def test_format_sql_aligns_multiple_and_conditions_under_anchor() -> None:
    from analytics_toolkit.sql_format import format_sql

    assert (
        format_sql("select * from t where x=1 and y=2 and label = 'A AND B'")
        == "SELECT\n"
        "    *\n"
        "FROM t\n"
        "WHERE 1=1\n"
        "      AND x = 1\n"
        "      AND y = 2\n"
        "      AND label = 'A AND B'"
    )


@pytest.mark.parametrize(
    ("keyword_case", "expected_sql"),
    [
        (
            "lower",
            "select\n"
            "    *\n"
            "from t\n"
            "where 1=1\n"
            "      and x = 1",
        ),
        (
            "capitalize",
            "Select\n"
            "    *\n"
            "From t\n"
            "Where 1=1\n"
            "      And x = 1",
        ),
    ],
)
def test_format_sql_applies_keyword_case_to_aligned_where_anchor(
    keyword_case: str,
    expected_sql: str,
) -> None:
    from analytics_toolkit.sql_format import format_sql

    assert (
        format_sql("select * from t where x=1", keyword_case=keyword_case)
        == expected_sql
    )


def test_format_sql_preserves_semicolon_with_aligned_where_anchor() -> None:
    from analytics_toolkit.sql_format import format_sql

    assert (
        format_sql("select * from t where x=1;")
        == "SELECT\n"
        "    *\n"
        "FROM t\n"
        "WHERE 1=1\n"
        "      AND x = 1;"
    )


@pytest.mark.parametrize(
    "sql",
    [
        "",
        "select 1; select 2",
        "select from",
    ],
)
def test_format_sql_rejects_invalid_inputs(sql: str) -> None:
    from analytics_toolkit.sql_format import format_sql

    with pytest.raises(ValueError):
        format_sql(sql)


@pytest.mark.parametrize(
    ("dialect", "sql", "expected_fragments"),
    [
        (
            "postgres",
            "select payload->>'name' as name from events where id::int = 1",
            ("payload", "CAST(id AS INT) = 1"),
        ),
        (
            "trino",
            (
                "select approx_distinct(user_id) from iceberg.web.events "
                "where event_date = DATE '2026-06-01'"
            ),
            ("APPROX_DISTINCT(user_id)", "event_date"),
        ),
        (
            "clickhouse",
            (
                "select countIf(x > 0) from events final "
                "where toDate(ts) = toDate('2026-06-01')"
            ),
            ("countIf(x > 0)", "FINAL"),
        ),
    ],
)
def test_format_sql_dialect_smoke_cases(
    dialect: str,
    sql: str,
    expected_fragments: tuple[str, ...],
) -> None:
    from analytics_toolkit.sql_format import format_sql

    formatted = format_sql(sql, dialect=dialect)

    for fragment in expected_fragments:
        assert fragment in formatted


def test_rewrite_with_ctes_extracts_derived_select() -> None:
    from analytics_toolkit.sql_format import rewrite_with_ctes

    assert (
        rewrite_with_ctes(
            "select s.user_id, s.revenue "
            "from ("
            "select user_id, sum(amount) as revenue "
            "from orders group by user_id"
            ") s "
            "where s.revenue > 100"
        )
        == "WITH cte_1 AS (\n"
        "    SELECT\n"
        "        user_id,\n"
        "        SUM(amount) AS revenue\n"
        "    FROM orders\n"
        "    GROUP BY\n"
        "        user_id\n"
        ")\n"
        "SELECT\n"
        "    s.user_id,\n"
        "    s.revenue\n"
        "FROM cte_1 AS s\n"
        "WHERE\n"
        "    s.revenue > 100"
    )


@pytest.mark.parametrize(
    "sql",
    [
        "select user_id from orders",
        "select user_id from orders where amount > (select avg(amount) from orders)",
    ],
)
def test_rewrite_with_ctes_fails_for_unsupported_queries(sql: str) -> None:
    from analytics_toolkit.sql_format import rewrite_with_ctes

    with pytest.raises(ValueError):
        rewrite_with_ctes(sql)
