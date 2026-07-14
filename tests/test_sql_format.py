from __future__ import annotations

import pytest
from analytics_toolkit import sql_format
from sqlglot import exp, parse_one
from sqlglot.errors import SqlglotError


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
        == "select *\n"
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
        == "select *\n"
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
        == "select *\n"
        "from t\n"
        f"{expected_where}"
    )


def test_format_sql_aligns_multiple_and_conditions_under_anchor() -> None:
    from analytics_toolkit.sql_format import format_sql

    assert (
        format_sql("select * from t where x=1 and y=2 and label = 'A AND B'")
        == "select *\n"
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
            "SELECT *\n"
            "FROM t\n"
            "WHERE 1=1\n"
            "      AND x = 1",
        ),
        (
            "lower",
            "select *\n"
            "from t\n"
            "where 1=1\n"
            "      and x = 1",
        ),
        (
            "capitalize",
            "Select *\n"
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
        == "select *\n"
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
        "select *\n"
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
        "select *\n"
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
        "SELECT *\n"
        "FROM b"
    )


def test_format_sql_adds_default_blank_line_before_unions() -> None:
    from analytics_toolkit.sql_format import format_sql

    assert (
        format_sql(
            "select * from a union all select * from b union select * from c"
        )
        == "select *\n"
        "from a\n"
        "\n"
        "union all\n"
        "select *\n"
        "from b\n"
        "\n"
        "union\n"
        "select *\n"
        "from c"
    )


def test_format_sql_can_disable_blank_lines_before_unions() -> None:
    from analytics_toolkit.sql_format import format_sql

    assert (
        format_sql(
            "select * from a union select * from b",
            union_blank_lines=0,
        )
        == "select *\n"
        "from a\n"
        "union\n"
        "select *\n"
        "from b"
    )


def test_format_sql_compacts_single_star_select_lists() -> None:
    from analytics_toolkit.sql_format import format_sql

    assert format_sql("select * from t") == "select *\nfrom t"
    assert format_sql("select t.* from t") == "select t.*\nfrom t"
    assert (
        format_sql("select distinct * from t")
        == "select distinct *\n"
        "from t"
    )
    assert (
        format_sql("select distinct t.* from t")
        == "select distinct t.*\n"
        "from t"
    )


def test_format_sql_keeps_multi_star_select_lists_expanded() -> None:
    from analytics_toolkit.sql_format import format_sql

    assert (
        format_sql("select a.*, b.* from a join b on a.id = b.id")
        == "select\n"
        "    a.*,\n"
        "    b.*\n"
        "from a\n"
        "join b\n"
        "  on a.id = b.id"
    )
    assert (
        format_sql("select *, x from t")
        == "select\n"
        "    *,\n"
        "    x\n"
        "from t"
    )
    assert (
        format_sql("select count(*) from t")
        == "select\n"
        "    COUNT(*)\n"
        "from t"
    )


def test_format_sql_preserves_group_by_expressions_with_star_projection() -> None:
    from analytics_toolkit.sql_format import format_sql

    assert (
        format_sql("select * from t group by a, b, c")
        == "select *\n"
        "from t\n"
        "group by a, b, c"
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
    "union_blank_lines",
    [-1, True, 1.5],
)
@pytest.mark.parametrize(
    "helper_name",
    ["format_sql", "rewrite_with_ctes", "gp_rewrite_to_temp_tables"],
)
def test_sql_format_helpers_reject_invalid_union_blank_lines(
    helper_name: str,
    union_blank_lines: object,
) -> None:
    from analytics_toolkit import sql_format

    with pytest.raises(ValueError, match="union_blank_lines"):
        getattr(sql_format, helper_name)(
            "select 1",
            union_blank_lines=union_blank_lines,
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
            "        select *\n"
            "        from tmp_1\n"
            "    )",
        ),
        (
            "select * from users where id in "
            "(select user_id from orders)",
            "id in (\n"
            "        select *\n"
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
        "SELECT *\n"
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


@pytest.mark.parametrize(
    ("helper_name", "kwargs", "message"),
    [
        ("format_sql", {"dialect": "oracle"}, "dialect"),
        ("format_sql", {"keyword_case": "title"}, "keyword_case"),
        ("format_sql", {"indent": 0}, "indent"),
        ("format_sql", {"indent": True}, "indent"),
        ("format_sql", {"where_anchor": "automatic"}, "where_anchor"),
        ("format_sql", {"group_by_format": None}, "group_by_format"),
        ("rewrite_with_ctes", {"strategy": "aggressive"}, "strategy"),
        ("rewrite_with_ctes", {"cte_prefix": "1cte"}, "cte_prefix"),
    ],
)
def test_sql_format_helpers_validate_public_options(
    helper_name: str,
    kwargs: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        getattr(sql_format, helper_name)("select 1", **kwargs)


@pytest.mark.parametrize(
    "helper_name",
    ["format_sql", "rewrite_with_ctes", "gp_rewrite_to_temp_tables"],
)
def test_sql_format_helpers_reject_non_string_sql(helper_name: str) -> None:
    with pytest.raises(ValueError, match="sql to be a string"):
        getattr(sql_format, helper_name)(None)


def test_rewrite_with_ctes_rejects_non_select_statement() -> None:
    with pytest.raises(ValueError, match="expects a SELECT"):
        sql_format.rewrite_with_ctes("delete from events")


def test_rewrite_with_ctes_skips_existing_generated_name() -> None:
    rewritten = sql_format.rewrite_with_ctes(
        "with cte_1 as (select 1 as id) "
        "select s.id from (select id from cte_1) s"
    )

    assert "cte_1 as (" in rewritten
    assert "cte_2 as (" in rewritten
    assert "from cte_2 as s" in rewritten


@pytest.mark.parametrize(
    ("sql", "message"),
    [
        (
            "with recursive c as (select 1 union all select * from c) "
            "select * from c",
            "recursive CTEs",
        ),
        (
            "with c(id) as (select id from events) select * from c",
            "CTE column aliases",
        ),
        (
            "select * from (select id from events) as s(id)",
            "derived-table column aliases",
        ),
        (
            "select * from events e where exists "
            "(select 1 from orders o where o.user_id = e.id)",
            "correlated subqueries",
        ),
    ],
)
def test_gp_rewrite_rejects_unsupported_select_shapes(
    sql: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        sql_format.gp_rewrite_to_temp_tables(sql)


def test_gp_rewrite_materializes_nested_select_and_avoids_name_collision() -> None:
    rewritten = sql_format.gp_rewrite_to_temp_tables(
        "with tmp_1 as (select id from events) "
        "select * from users where id in (select id from tmp_1)"
    )

    assert "create temporary table tmp_1 as" in rewritten
    assert "create temporary table tmp_2 as" in rewritten
    assert "from tmp_2" in rewritten


def test_gp_rewrite_capitalizes_generated_block_keywords() -> None:
    rewritten = sql_format.gp_rewrite_to_temp_tables(
        "select * from (select id from events) s",
        keyword_case="capitalize",
    )

    assert rewritten.startswith("Drop Table If Exists s;")
    assert "Create Temporary Table s As (" in rewritten
    assert "Distributed Randomly;" in rewritten


def test_sql_format_clause_mapping_handles_duplicate_aliases() -> None:
    assert (
        sql_format.format_sql("select a as x, b as x from t order by x")
        == "select\n    a as x,\n    b as x\nfrom t\norder by x"
    )


def test_sql_format_internal_layout_edges() -> None:
    assert sql_format._compact_targeted_clause_layout(
        "GROUP BY\nnext",
        [sql_format._ClauseCompactionTarget("GROUP BY", "1")],
    ) == "GROUP BY\nnext"
    assert sql_format._compact_targeted_clause_layout(
        "GROUP BY\n    2",
        [sql_format._ClauseCompactionTarget("GROUP BY", "1")],
    ) == "GROUP BY\n    2"
    assert sql_format._compact_clause_item_lines(
        ["a +", "b,", ", c", "  + d"]
    ) == "a + b, c + d"
    assert sql_format._normalize_union_separator_layout(
        "select 1\n\n\nunion\nselect 2",
        1,
    ) == "select 1\n\nunion\nselect 2"


def test_sql_format_internal_join_layout_edges() -> None:
    assert sql_format._normalize_join_condition_layout("ON a.id = b.id") == (
        "ON a.id = b.id"
    )
    assert sql_format._normalize_join_condition_layout("AND a.id = b.id") == (
        "AND a.id = b.id"
    )
    assert sql_format._normalize_join_condition_layout(
        "where true\nAND a.id = b.id"
    ) == "where true\nAND a.id = b.id"
    assert sql_format._split_join_condition_line("ON") == ["ON"]
    assert sql_format._split_join_condition_line("ON ") == ["ON "]
    assert sql_format._previous_non_empty_line(["one", "", ""], 3) == "one"


def test_sql_format_internal_where_and_quote_layout_edges() -> None:
    assert sql_format._normalize_where_anchor_layout(
        "WHERE\n    x = 1",
        "1=1",
    ) == "WHERE\n    x = 1"
    assert sql_format._split_anchor_and_conditions("1 = 1", "1 = 1") == []
    assert sql_format._split_anchor_and_conditions(
        "1 = 1 OR x = 1",
        "1 = 1",
    ) == ["OR x = 1"]
    assert sql_format._split_top_level_and_conditions(
        "x = 'it''s AND ok' AND y = \"A AND B\" "
        "AND z = `C AND D` AND q = [E AND F]"
    ) == [
        "x = 'it''s AND ok'",
        'y = "A AND B"',
        "z = `C AND D`",
        "q = [E AND F]",
    ]


def test_sql_format_internal_temp_reference_shape_checks() -> None:
    temp_names = {"tmp"}
    invalid_sql = [
        "with c as (select 1) select * from tmp",
        "select * from tmp join other on tmp.id = other.id",
        "select * from tmp where id = 1",
        "select id from tmp",
        "select *",
    ]
    for sql in invalid_sql:
        expression = parse_one(sql)
        assert not sql_format._is_temp_reference_select(
            expression,
            temp_names=temp_names,
        )

    assert sql_format._is_temp_reference_select(
        parse_one("select * from tmp"),
        temp_names=temp_names,
    )


def test_sql_format_internal_relation_and_join_column_edges() -> None:
    assert sql_format._relation_source_names(None) == set()
    expression = parse_one("select * from (select 1) s")
    subquery = expression.args[sql_format._from_arg_name()].this
    assert sql_format._relation_source_names(subquery) == {"s"}
    assert sql_format._relation_source_names(exp.Literal.number(1)) == set()

    both_temp = exp.EQ(
        this=exp.column("left_id", table="tmp"),
        expression=exp.column("right_id", table="tmp"),
    )
    neither_temp = exp.EQ(
        this=exp.column("left_id", table="a"),
        expression=exp.column("right_id", table="b"),
    )
    assert sql_format._join_column_for_temp(
        both_temp,
        consumer_names={"tmp"},
        dialect=None,
    ) is None
    assert sql_format._join_column_for_temp(
        neither_temp,
        consumer_names={"tmp"},
        dialect=None,
    ) is None
    assert sql_format._column_name_for_temp(
        exp.Literal.number(1),
        consumer_names={"tmp"},
        dialect=None,
    ) is None


def test_sql_format_internal_validation_and_parse_edges(monkeypatch) -> None:
    with pytest.raises(ValueError, match="temp alias"):
        sql_format._validate_temp_table_name("bad-name", label="temp alias")

    monkeypatch.setattr(sql_format, "parse", lambda sql, read: [])
    with pytest.raises(ValueError, match="exactly one"):
        sql_format._parse_expression(
            "select 1",
            dialect=None,
            operation="test",
        )


def test_sql_format_internal_ordinal_mapping_edges() -> None:
    select = parse_one("select a from t")
    mapping = sql_format._select_ordinal_mapping(select, dialect=None)
    select.set("order", exp.Order(expressions=[exp.column("a")]))
    sql_format._replace_order_by_items(select, mapping=mapping, dialect=None)
    assert select.args["order"].expressions[0].this == "1"
    assert sql_format._bare_identifier_key(exp.to_identifier("Mixed")) == "mixed"
    assert sql_format._bare_identifier_key(exp.Literal.number(1)) is None
    assert sql_format._compact_clause_item_lines(["a", ", b"]) == "a, b"


def test_sql_format_internal_where_normalization_edges() -> None:
    select = parse_one("select * from t")
    select.set("where", exp.Where())
    sql_format._normalize_where_clauses(select, "1=1")
    assert select.args["where"].this is None

    only_anchor = parse_one("select * from t where true")
    sql_format._normalize_where_clauses(only_anchor, "first_condition")
    assert only_anchor.args["where"].this.sql() == "TRUE"

    numeric_anchor = parse_one("select * from t where 1 = 1")
    sql_format._normalize_where_clauses(numeric_anchor, "true")
    assert numeric_anchor.args["where"].this.sql() == "TRUE"

    assert not sql_format._is_artificial_anchor_condition(exp.false())
    assert not sql_format._is_artificial_anchor_condition(
        exp.EQ(this=exp.Literal.number(1), expression=exp.Literal.number(2))
    )
    assert not sql_format._is_artificial_anchor_condition(exp.column("x"))


def test_rewrite_with_ctes_rejects_nested_derived_tables() -> None:
    with pytest.raises(ValueError, match="nested derived subqueries"):
        sql_format.rewrite_with_ctes(
            "select * from (select * from (select id from events) inner_s) outer_s"
        )


def test_sql_format_internal_derived_subquery_shape_edges() -> None:
    expression = parse_one("select * from (select id from events)")
    extracted = sql_format._extract_supported_ctes(expression, cte_prefix="cte")
    assert extracted[0].alias_or_name == "cte_1"

    sampled = parse_one("select * from (select id from events) s")
    sampled_subquery = next(sampled.find_all(exp.Subquery))
    sampled_subquery.set("sample", exp.TableSample(percent=exp.Literal.number(1)))
    assert not sql_format._is_supported_derived_subquery(sampled_subquery)

    pivoted = parse_one("select * from (select id from events) s")
    pivoted_subquery = next(pivoted.find_all(exp.Subquery))
    pivoted_subquery.set("pivots", [exp.Pivot()])
    assert not sql_format._is_supported_derived_subquery(pivoted_subquery)

    detached = exp.Subquery(this=exp.select("id"))
    assert not sql_format._is_supported_derived_subquery(detached)


def test_sql_format_internal_planner_traversal_and_distribution_edges() -> None:
    planner = sql_format._GpTempTablePlanner(
        dialect=None,
        temp_prefix="tmp",
        group_by_format="ordinal",
        order_by_format="ordinal",
        keyword_case="lower",
        indent=4,
        cte_blank_lines=1,
        union_blank_lines=1,
    )
    planner._rewrite_child_expression(parse_one("select id from events"))
    assert planner.temp_tables[0].name == "tmp_1"

    consumers = [
        parse_one("select * from tmp cross join other"),
        parse_one("select * from tmp join other on tmp.id > other.id"),
        parse_one(
            "select * from tmp join other "
            "on other.id = tmp.id and other.id = tmp.id"
        ),
    ]
    assert planner._distributed_columns(
        "missing",
        consumer_expressions=consumers,
    ) == []
    assert planner._distributed_columns(
        "tmp",
        consumer_expressions=consumers,
    ) == ["id"]

    joined = parse_one("select * from tmp join other on tmp.id = other.id")
    assert sql_format._local_select_source_names(joined) == {"tmp", "other"}


def test_sql_format_internal_right_join_column_and_indentation_edges() -> None:
    right_temp = exp.EQ(
        this=exp.column("left_id", table="other"),
        expression=exp.column("right_id", table="tmp"),
    )
    assert sql_format._join_column_for_temp(
        right_temp,
        consumer_names={"tmp"},
        dialect=None,
    ) == "right_id"

    assert sql_format._normalize_leading_comma_indentation(
        "SELECT\n        a",
        4,
    ) == "SELECT\n    a"
    assert sql_format._normalize_leading_comma_indentation(
        "SELECT\n    , a",
        4,
    ) == "SELECT\n    , a"
    assert sql_format._normalize_join_condition_layout(
        "join b\n  ON a.id = b.id\n    AND a.kind = b.kind"
    ) == "join b\n  ON a.id = b.id\n AND a.kind = b.kind"


def test_format_sql_converts_sqlglot_render_errors(monkeypatch) -> None:
    class BrokenExpression(exp.Expression):
        def sql(self, *args, **kwargs) -> str:
            return (_ for _ in ()).throw(SqlglotError("render failed"))

    monkeypatch.setattr(
        sql_format,
        "_prepare_group_order_rendering",
        lambda *args, **kwargs: (BrokenExpression(), []),
    )

    with pytest.raises(ValueError, match="format_sql could not render SQL"):
        sql_format.format_sql("select 1")


def test_sql_format_converts_matching_and_distribution_render_errors(
    monkeypatch,
) -> None:
    expression = exp.column("value")
    monkeypatch.setattr(
        expression,
        "sql",
        lambda *args, **kwargs: (_ for _ in ()).throw(SqlglotError("match failed")),
    )
    with pytest.raises(ValueError, match="render SQL expression for matching"):
        sql_format._expression_match_key(expression, dialect=None)

    identifier = exp.to_identifier("id")
    column = exp.Column(this=identifier, table=exp.to_identifier("tmp"))
    monkeypatch.setattr(
        identifier,
        "sql",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            SqlglotError("distribution failed")
        ),
    )
    with pytest.raises(ValueError, match="render distribution column"):
        sql_format._column_name_for_temp(
            column,
            consumer_names={"tmp"},
            dialect=None,
        )


def test_format_sql_traverses_cte_and_outer_select_ordinals() -> None:
    formatted = sql_format.format_sql(
        "with totals as ("
        "select account_id, sum(amount) as amount from payments "
        "group by account_id order by amount"
        ") select amount, count(*) as n from totals "
        "group by amount order by n",
    )

    assert formatted.count("group by 1") == 2
    assert formatted.count("order by 2") == 2

    group_only = sql_format.format_sql(
        "with grouped as (select a from t group by a) "
        "select a from grouped group by a",
        order_by_format="expressions",
    )
    assert group_only.count("group by 1") == 2


def test_format_sql_ordinal_mapping_keeps_first_duplicate_projection() -> None:
    formatted = sql_format.format_sql(
        "select account_id, account_id from payments group by account_id"
    )

    assert formatted.endswith("group by 1")


def test_sql_format_ordinal_mapping_handles_empty_alias_and_unmatched_qualifier() -> None:
    empty_alias = exp.Alias(
        this=exp.column("account_id"),
        alias=exp.to_identifier(""),
    )
    assert sql_format._projection_alias_key(empty_alias) is None

    select = parse_one("select account_id from payments")
    mapping = sql_format._select_ordinal_mapping(select, dialect=None)
    qualified = exp.column("missing", table="payments")
    assert (
        sql_format._select_position_for_clause_expression(
            qualified,
            mapping=mapping,
            dialect=None,
        )
        is None
    )


def test_sql_format_replaces_multiple_non_ordered_items() -> None:
    select = parse_one("select a, b from t")
    mapping = sql_format._select_ordinal_mapping(select, dialect=None)
    select.set(
        "order",
        exp.Order(expressions=[exp.column("missing"), exp.column("a")]),
    )

    sql_format._replace_order_by_items(select, mapping=mapping, dialect=None)

    assert [item.sql() for item in select.args["order"].expressions] == ["missing", "1"]


def test_format_sql_mixes_expression_grouping_with_ordinal_ordering() -> None:
    formatted = sql_format.format_sql(
        "select a, b from t group by a, b order by b",
        group_by_format="expressions",
    )

    assert "group by\n    a,\n    b" in formatted
    assert formatted.endswith("order by 2")


def test_sql_format_compaction_stops_at_blank_line_and_handles_final_comma() -> None:
    target = sql_format._ClauseCompactionTarget("GROUP BY", "1")
    assert sql_format._compact_targeted_clause_layout(
        "GROUP BY\n\n    1",
        [target],
    ) == "GROUP BY\n\n    1"
    assert sql_format._compact_clause_item_lines(["1,"]) == "1"


def test_rewrite_with_ctes_reports_injected_residual_select(
    monkeypatch,
) -> None:
    expression = parse_one("select * from (select id from events) s")
    monkeypatch.setattr(exp.Subquery, "replace", lambda self, expression: self)

    with pytest.raises(ValueError, match="confidently rewrite all SELECT subqueries"):
        sql_format._extract_supported_ctes(expression, cte_prefix="cte")


def test_sqlglot_with_and_from_argument_name_compatibility(monkeypatch) -> None:
    current_arg_types = exp.Select.arg_types
    legacy_arg_types = {
        key: value for key, value in current_arg_types.items() if key not in {"with_", "from_"}
    }
    legacy_arg_types.update({"with": False, "from": False})
    monkeypatch.setattr(exp.Select, "arg_types", legacy_arg_types)

    assert sql_format._with_arg_name() == "with"
    assert sql_format._from_arg_name() == "from"

    select = exp.Select(expressions=[exp.Star()])
    select.set("from", exp.From(this=exp.to_table("tmp")))
    assert sql_format._is_temp_reference_select(select, temp_names={"tmp"})


def test_sql_format_local_sources_support_select_without_from() -> None:
    assert sql_format._local_select_source_names(parse_one("select 1")) == set()


def test_gp_planner_postconditions_reject_residual_query_shapes() -> None:
    planner = sql_format._GpTempTablePlanner(
        dialect=None,
        temp_prefix="tmp",
        group_by_format="ordinal",
        order_by_format="ordinal",
        keyword_case="lower",
        indent=4,
        cte_blank_lines=1,
        union_blank_lines=1,
    )

    remaining_with = parse_one("with c as (select 1) select * from c")
    with pytest.raises(ValueError, match="remove every WITH clause"):
        planner.validate_complete_rewrite(remaining_with)

    nested_select = exp.Select(expressions=[exp.Select(expressions=[exp.Literal.number(1)])])
    with pytest.raises(ValueError, match="nested SELECT queries"):
        planner.validate_complete_rewrite(nested_select)

    residual_subquery = exp.select(exp.Subquery(this=exp.select("id").from_("events")))
    with pytest.raises(ValueError, match="nested SELECT queries"):
        planner.validate_complete_rewrite(residual_subquery)

    residual_exists = exp.select(exp.Exists(this=exp.select("id").from_("events")))
    with pytest.raises(ValueError, match="nested SELECT queries"):
        planner.validate_complete_rewrite(residual_exists)


def test_gp_planner_rejects_non_select_cte_body() -> None:
    planner = sql_format._GpTempTablePlanner(
        dialect=None,
        temp_prefix="tmp",
        group_by_format="ordinal",
        order_by_format="ordinal",
        keyword_case="lower",
        indent=4,
        cte_blank_lines=1,
        union_blank_lines=1,
    )
    select = exp.select("*")
    select.set(
        sql_format._with_arg_name(),
        exp.With(
            expressions=[
                exp.CTE(
                    this=exp.Delete(this=exp.to_table("events")),
                    alias=exp.TableAlias(this=exp.to_identifier("c")),
                )
            ]
        ),
    )

    with pytest.raises(ValueError, match="only supports SELECT CTEs"):
        planner.rewrite_select(select)


def test_gp_planner_child_loop_skips_with_and_non_expression_items() -> None:
    planner = sql_format._GpTempTablePlanner(
        dialect=None,
        temp_prefix="tmp",
        group_by_format="ordinal",
        order_by_format="ordinal",
        keyword_case="lower",
        indent=4,
        cte_blank_lines=1,
        union_blank_lines=1,
    )
    with_expression = exp.With(expressions=[])
    select = exp.select("1")
    select.set(sql_format._with_arg_name(), with_expression)
    planner._rewrite_expression_children(select)
    assert select.args[sql_format._with_arg_name()] is with_expression

    container = exp.Expression()
    container.set("items", [exp.Literal.number(1), "not-an-expression"])
    planner._rewrite_expression_children(container)
    assert container.args["items"][1] == "not-an-expression"


def test_gp_planner_distribution_ignores_unrelated_join_equality() -> None:
    planner = sql_format._GpTempTablePlanner(
        dialect=None,
        temp_prefix="tmp",
        group_by_format="ordinal",
        order_by_format="ordinal",
        keyword_case="lower",
        indent=4,
        cte_blank_lines=1,
        union_blank_lines=1,
    )
    consumer = parse_one(
        "select * from tmp join other on left_side.id = right_side.id"
    )

    assert planner._distributed_columns(
        "tmp",
        consumer_expressions=[consumer],
    ) == []
