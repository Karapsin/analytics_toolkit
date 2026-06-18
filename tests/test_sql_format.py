from __future__ import annotations

import pytest


def test_sql_format_is_importable_from_root_package() -> None:
    from analytics_toolkit import sql_format

    assert sql_format.format_sql("select 1") == "select\n    1"


def test_format_sql_basic_pretty_output() -> None:
    from analytics_toolkit.sql_format import format_sql

    assert (
        format_sql("select a,b from t order by b desc")
        == "select\n"
        "    a,\n"
        "    b\n"
        "from t\n"
        "order by 2 desc"
    )


def test_format_sql_aligns_join_condition_with_join_keyword() -> None:
    from analytics_toolkit.sql_format import format_sql

    assert (
        format_sql(
            "select * from a "
            "join b on a.id = b.id "
            "and a.kind = b.kind"
        )
        == "select\n"
        "    *\n"
        "from a\n"
        "join b\n"
        "  on a.id = b.id\n"
        " and a.kind = b.kind"
    )


def test_format_sql_defaults_group_by_to_ordinals() -> None:
    from analytics_toolkit.sql_format import format_sql

    assert (
        format_sql("select a,b,count(*) from t group by a,b")
        == "select\n"
        "    a,\n"
        "    b,\n"
        "    COUNT(*)\n"
        "from t\n"
        "group by 1, 2"
    )


def test_format_sql_defaults_order_by_to_ordinals_with_direction() -> None:
    from analytics_toolkit.sql_format import format_sql

    assert (
        format_sql("select a,b from t order by a,b desc")
        == "select\n"
        "    a,\n"
        "    b\n"
        "from t\n"
        "order by 1, 2 desc"
    )


def test_format_sql_can_preserve_expression_group_and_order_output() -> None:
    from analytics_toolkit.sql_format import format_sql

    assert (
        format_sql(
            "select a,b from t group by a,b order by a,b desc",
            group_by_format="expressions",
            order_by_format="expressions",
        )
        == "select\n"
        "    a,\n"
        "    b\n"
        "from t\n"
        "group by\n"
        "    a,\n"
        "    b\n"
        "order by\n"
        "    a,\n"
        "    b desc"
    )


def test_format_sql_matches_group_by_expression_and_order_by_alias() -> None:
    from analytics_toolkit.sql_format import format_sql

    assert (
        format_sql(
            "select a + 1 as day, sum(x) as sx "
            "from t group by a + 1 order by sx desc"
        )
        == "select\n"
        "    a + 1 as day,\n"
        "    SUM(x) as sx\n"
        "from t\n"
        "group by 1\n"
        "order by 2 desc"
    )


def test_format_sql_matches_group_by_alias_and_order_by_expression() -> None:
    from analytics_toolkit.sql_format import format_sql

    assert (
        format_sql(
            "select a + 1 as day, count(*) as n "
            "from t group by day order by a + 1"
        )
        == "select\n"
        "    a + 1 as day,\n"
        "    COUNT(*) as n\n"
        "from t\n"
        "group by 1\n"
        "order by 1"
    )


def test_format_sql_keeps_existing_group_and_order_ordinals() -> None:
    from analytics_toolkit.sql_format import format_sql

    assert (
        format_sql("select a,b from t group by 1 order by 2 desc")
        == "select\n"
        "    a,\n"
        "    b\n"
        "from t\n"
        "group by 1\n"
        "order by 2 desc"
    )


def test_format_sql_preserves_unmatched_group_and_order_expressions() -> None:
    from analytics_toolkit.sql_format import format_sql

    assert (
        format_sql("select a from t group by b order by c desc")
        == "select\n"
        "    a\n"
        "from t\n"
        "group by b\n"
        "order by c desc"
    )


def test_format_sql_rejects_invalid_group_and_order_format_values() -> None:
    from analytics_toolkit.sql_format import format_sql

    with pytest.raises(ValueError, match="group_by_format"):
        format_sql("select 1", group_by_format="compact")
    with pytest.raises(ValueError, match="order_by_format"):
        format_sql("select 1", order_by_format="compact")


def test_format_sql_uppercase_keyword_case_compacts_ordinal_clauses() -> None:
    from analytics_toolkit.sql_format import format_sql

    assert (
        format_sql(
            "select a,b from t group by a,b order by a desc",
            keyword_case="upper",
        )
        == "SELECT\n"
        "    a,\n"
        "    b\n"
        "FROM t\n"
        "GROUP BY 1, 2\n"
        "ORDER BY 1 DESC"
    )


def test_format_sql_adds_default_where_anchor() -> None:
    from analytics_toolkit.sql_format import format_sql

    assert (
        format_sql("select * from t where x=1")
        == "select\n"
        "    *\n"
        "from t\n"
        "where 1=1\n"
        "      and x = 1"
    )


def test_format_sql_supports_trailing_and_leading_commas() -> None:
    from analytics_toolkit.sql_format import format_sql

    assert (
        format_sql("select a,b from t", leading_commas=False)
        == "select\n"
        "    a,\n"
        "    b\n"
        "from t"
    )
    assert (
        format_sql("select a,b from t", leading_commas=True)
        == "select\n"
        "    a\n"
        "    , b\n"
        "from t"
    )


@pytest.mark.parametrize(
    ("where_anchor", "expected_where"),
    [
        ("1=1", "where 1=1\n      and x = 1"),
        ("true", "where true\n      and x = 1"),
        ("first_condition", "where\n    x = 1"),
        ("preserve", "where\n    1 = 1 and x = 1"),
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
        == "select\n"
        "    *\n"
        "from t\n"
        f"{expected_where}"
    )


def test_format_sql_aligns_multiple_and_conditions_under_anchor() -> None:
    from analytics_toolkit.sql_format import format_sql

    assert (
        format_sql("select * from t where x=1 and y=2 and label = 'A AND B'")
        == "select\n"
        "    *\n"
        "from t\n"
        "where 1=1\n"
        "      and x = 1\n"
        "      and y = 2\n"
        "      and label = 'A AND B'"
    )


@pytest.mark.parametrize(
    ("keyword_case", "expected_sql"),
    [
        (
            "upper",
            "SELECT\n"
            "    *\n"
            "FROM t\n"
            "WHERE 1=1\n"
            "      AND x = 1",
        ),
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
        == "select\n"
        "    *\n"
        "from t\n"
        "where 1=1\n"
        "      and x = 1;"
    )


def test_format_sql_adds_default_blank_line_between_ctes() -> None:
    from analytics_toolkit.sql_format import format_sql

    assert (
        format_sql(
            "with a as (select 1 as x), "
            "b as (select x from a), "
            "c as (select x from b) "
            "select * from c"
        )
        == "with a as (\n"
        "    select\n"
        "        1 as x\n"
        "),\n"
        "\n"
        "b as (\n"
        "    select\n"
        "        x\n"
        "    from a\n"
        "),\n"
        "\n"
        "c as (\n"
        "    select\n"
        "        x\n"
        "    from b\n"
        ")\n"
        "select\n"
        "    *\n"
        "from c"
    )


def test_format_sql_can_disable_blank_lines_between_ctes() -> None:
    from analytics_toolkit.sql_format import format_sql

    assert (
        format_sql(
            "with a as (select 1 as x), "
            "b as (select x from a) "
            "select * from b",
            cte_blank_lines=0,
        )
        == "with a as (\n"
        "    select\n"
        "        1 as x\n"
        "),\n"
        "b as (\n"
        "    select\n"
        "        x\n"
        "    from a\n"
        ")\n"
        "select\n"
        "    *\n"
        "from b"
    )


def test_format_sql_cte_spacing_preserves_keyword_case() -> None:
    from analytics_toolkit.sql_format import format_sql

    assert (
        format_sql(
            "with a as (select 1 as x), b as (select x from a) select * from b",
            keyword_case="upper",
        )
        == "WITH a AS (\n"
        "    SELECT\n"
        "        1 AS x\n"
        "),\n"
        "\n"
        "b AS (\n"
        "    SELECT\n"
        "        x\n"
        "    FROM a\n"
        ")\n"
        "SELECT\n"
        "    *\n"
        "FROM b"
    )


def test_rewrite_with_ctes_exposes_cte_blank_lines() -> None:
    from analytics_toolkit.sql_format import rewrite_with_ctes

    assert (
        rewrite_with_ctes(
            "with old as (select 1 as x) "
            "select s.x from (select x from old) s",
            cte_blank_lines=0,
        )
        == "with old as (\n"
        "    select\n"
        "        1 as x\n"
        "),\n"
        "cte_1 as (\n"
        "    select\n"
        "        x\n"
        "    from old\n"
        ")\n"
        "select\n"
        "    s.x\n"
        "from cte_1 as s"
    )


@pytest.mark.parametrize(
    "cte_blank_lines",
    [-1, True, 1.5],
)
@pytest.mark.parametrize(
    "helper_name",
    ["format_sql", "rewrite_with_ctes", "gp_rewrite_to_temp_tables"],
)
def test_sql_format_helpers_reject_invalid_cte_blank_lines(
    helper_name: str,
    cte_blank_lines: object,
) -> None:
    from analytics_toolkit import sql_format

    with pytest.raises(ValueError, match="cte_blank_lines"):
        getattr(sql_format, helper_name)(
            "select 1",
            cte_blank_lines=cte_blank_lines,
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
            ("payload", "CAST(id as int) = 1"),
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
            ("countIf(x > 0)", "final"),
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
        == "with cte_1 as (\n"
        "    select\n"
        "        user_id,\n"
        "        SUM(amount) as revenue\n"
        "    from orders\n"
        "    group by 1\n"
        ")\n"
        "select\n"
        "    s.user_id,\n"
        "    s.revenue\n"
        "from cte_1 as s\n"
        "where\n"
        "    s.revenue > 100"
    )


def test_rewrite_with_ctes_propagates_group_and_order_format_options() -> None:
    from analytics_toolkit.sql_format import rewrite_with_ctes

    assert (
        rewrite_with_ctes(
            "select s.user_id, s.revenue "
            "from ("
            "select user_id, sum(amount) as revenue "
            "from orders group by user_id order by revenue desc"
            ") s",
            group_by_format="expressions",
            order_by_format="expressions",
        )
        == "with cte_1 as (\n"
        "    select\n"
        "        user_id,\n"
        "        SUM(amount) as revenue\n"
        "    from orders\n"
        "    group by\n"
        "        user_id\n"
        "    order by\n"
        "        revenue desc\n"
        ")\n"
        "select\n"
        "    s.user_id,\n"
        "    s.revenue\n"
        "from cte_1 as s"
    )


def test_rewrite_with_ctes_supports_uppercase_keyword_case() -> None:
    from analytics_toolkit.sql_format import rewrite_with_ctes

    assert (
        rewrite_with_ctes(
            "select s.user_id from (select user_id from orders) s",
            keyword_case="upper",
        )
        == "WITH cte_1 AS (\n"
        "    SELECT\n"
        "        user_id\n"
        "    FROM orders\n"
        ")\n"
        "SELECT\n"
        "    s.user_id\n"
        "FROM cte_1 AS s"
    )


def test_gp_rewrite_to_temp_tables_materializes_cte_names() -> None:
    from analytics_toolkit.sql_format import gp_rewrite_to_temp_tables

    assert (
        gp_rewrite_to_temp_tables(
            "with customer_orders as ("
            "select user_id, sum(amount) as revenue from orders group by user_id"
            ") "
            "select u.id, customer_orders.revenue "
            "from customer_orders join users u "
            "on customer_orders.user_id = u.id"
        )
        == "drop table if exists customer_orders;\n"
        "create temporary table customer_orders as (\n"
        "    select\n"
        "        user_id,\n"
        "        SUM(amount) as revenue\n"
        "    from orders\n"
        "    group by 1\n"
        ") distributed by (user_id);\n"
        "analyze customer_orders;\n"
        "\n"
        "select\n"
        "    u.id,\n"
        "    customer_orders.revenue\n"
        "from customer_orders\n"
        "join users as u\n"
        "  on customer_orders.user_id = u.id"
    )


def test_gp_rewrite_to_temp_tables_materializes_derived_aliases() -> None:
    from analytics_toolkit.sql_format import gp_rewrite_to_temp_tables

    assert (
        gp_rewrite_to_temp_tables(
            "select u.id, s.revenue "
            "from ("
            "select user_id, sum(amount) as revenue "
            "from orders group by user_id"
            ") s "
            "join users u on s.user_id = u.id"
        )
        == "drop table if exists s;\n"
        "create temporary table s as (\n"
        "    select\n"
        "        user_id,\n"
        "        SUM(amount) as revenue\n"
        "    from orders\n"
        "    group by 1\n"
        ") distributed by (user_id);\n"
        "analyze s;\n"
        "\n"
        "select\n"
        "    u.id,\n"
        "    s.revenue\n"
        "from s\n"
        "join users as u\n"
        "  on s.user_id = u.id"
    )


def test_gp_rewrite_to_temp_tables_propagates_clause_formats() -> None:
    from analytics_toolkit.sql_format import gp_rewrite_to_temp_tables

    assert (
        gp_rewrite_to_temp_tables(
            "select u.id, s.revenue "
            "from ("
            "select user_id, sum(amount) as revenue "
            "from orders group by user_id order by revenue desc"
            ") s "
            "join users u on s.user_id = u.id",
            group_by_format="expressions",
            order_by_format="expressions",
        )
        == "drop table if exists s;\n"
        "create temporary table s as (\n"
        "    select\n"
        "        user_id,\n"
        "        SUM(amount) as revenue\n"
        "    from orders\n"
        "    group by\n"
        "        user_id\n"
        "    order by\n"
        "        revenue desc\n"
        ") distributed by (user_id);\n"
        "analyze s;\n"
        "\n"
        "select\n"
        "    u.id,\n"
        "    s.revenue\n"
        "from s\n"
        "join users as u\n"
        "  on s.user_id = u.id"
    )


@pytest.mark.parametrize(
    ("sql", "expected_final_predicate"),
    [
        (
            "select * from users where amount > "
            "(select avg(amount) from orders)",
            "amount > (\n"
            "        select\n"
            "            *\n"
            "        from tmp_1\n"
            "    )",
        ),
        (
            "select * from users where id in "
            "(select user_id from orders)",
            "id in (\n"
            "        select\n"
            "            *\n"
            "        from tmp_1\n"
            "    )",
        ),
    ],
)
def test_gp_rewrite_to_temp_tables_generates_names_for_scalar_and_predicate_subqueries(
    sql: str,
    expected_final_predicate: str,
) -> None:
    from analytics_toolkit.sql_format import gp_rewrite_to_temp_tables

    rewritten = gp_rewrite_to_temp_tables(sql)

    assert rewritten.startswith(
        "drop table if exists tmp_1;\n"
        "create temporary table tmp_1 as (\n"
    )
    assert ") distributed randomly;\nanalyze tmp_1;" in rewritten
    assert expected_final_predicate in rewritten


def test_gp_rewrite_to_temp_tables_uppercase_keyword_case() -> None:
    from analytics_toolkit.sql_format import gp_rewrite_to_temp_tables

    assert (
        gp_rewrite_to_temp_tables(
            "select * from (select user_id from orders) s "
            "join users u on s.user_id = u.id",
            keyword_case="upper",
        )
        == "DROP TABLE IF EXISTS s;\n"
        "CREATE TEMPORARY TABLE s AS (\n"
        "    SELECT\n"
        "        user_id\n"
        "    FROM orders\n"
        ") DISTRIBUTED BY (user_id);\n"
        "ANALYZE s;\n"
        "\n"
        "SELECT\n"
        "    *\n"
        "FROM s\n"
        "JOIN users AS u\n"
        "  ON s.user_id = u.id"
    )


@pytest.mark.parametrize(
    "sql",
    [
        "",
        "select 1; select 2",
        "delete from users",
        "select * from orders",
        "with c as (select id from orders), c as (select id from users) "
        "select * from c",
        "select * from (select id from orders) s "
        "join (select id from users) s on s.id = s.id",
        "select * from users where exists ("
        "select 1 from orders o where o.user_id = users.id"
        ")",
    ],
)
def test_gp_rewrite_to_temp_tables_rejects_invalid_or_unsafe_input(sql: str) -> None:
    from analytics_toolkit.sql_format import gp_rewrite_to_temp_tables

    with pytest.raises(ValueError):
        gp_rewrite_to_temp_tables(sql)


def test_gp_rewrite_to_temp_tables_rejects_invalid_prefix() -> None:
    from analytics_toolkit.sql_format import gp_rewrite_to_temp_tables

    with pytest.raises(ValueError):
        gp_rewrite_to_temp_tables(
            "select * from users where id in (select user_id from orders)",
            temp_prefix="1tmp",
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
