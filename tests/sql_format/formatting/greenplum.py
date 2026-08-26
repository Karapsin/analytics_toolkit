from __future__ import annotations

from tests.sql_format._support.formatting import (
    exp,
    parse_one,
    pytest,
    sql_format,
)


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
    consumer = parse_one("select * from tmp join other on left_side.id = right_side.id")

    assert (
        planner._distributed_columns(
            "tmp",
            consumer_expressions=[consumer],
        )
        == []
    )


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


def test_gp_planner_safely_traverses_non_query_set_operands() -> None:
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

    planner._rewrite_set_operand(exp.Literal.number(1))
    planner._rewrite_set_operand(exp.Subquery(this=exp.to_table("events")))

    assert planner.temp_tables == []


def test_gp_rewrite_capitalizes_generated_block_keywords() -> None:
    rewritten = sql_format.gp_rewrite_to_temp_tables(
        "select * from (select id from events) s",
        keyword_case="capitalize",
    )

    assert rewritten.startswith("Drop Table If Exists s;")
    assert "Create Temporary Table s As (" in rewritten
    assert "Distributed Randomly;" in rewritten


def test_gp_rewrite_materializes_nested_select_and_avoids_name_collision() -> None:
    rewritten = sql_format.gp_rewrite_to_temp_tables(
        "with tmp_1 as (select id from events) "
        "select * from users where id in (select id from tmp_1)"
    )

    assert "create temporary table tmp_1 as" in rewritten
    assert "create temporary table tmp_2 as" in rewritten
    assert "from tmp_2" in rewritten


@pytest.mark.parametrize(
    ("sql", "message"),
    [
        (
            "with recursive c as (select 1 union all select * from c) select * from c",
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
            "select * from events e where exists (select 1 from orders o where o.user_id = e.id)",
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


@pytest.mark.parametrize(
    ("sql", "expected_final_predicate"),
    [
        (
            "select * from users where amount > (select avg(amount) from orders)",
            "amount > (\n        select *\n        from tmp_1\n    )",
        ),
        (
            "select * from users where id in (select user_id from orders)",
            "id in (\n        select *\n        from tmp_1\n    )",
        ),
    ],
)
def test_gp_rewrite_to_temp_tables_generates_names_for_scalar_and_predicate_subqueries(
    sql: str,
    expected_final_predicate: str,
) -> None:
    from analytics_toolkit.sql_format import gp_rewrite_to_temp_tables

    rewritten = gp_rewrite_to_temp_tables(sql)

    assert rewritten.startswith("drop table if exists tmp_1;\ncreate temporary table tmp_1 as (\n")
    assert ") distributed randomly;\nanalyze tmp_1;" in rewritten
    assert expected_final_predicate in rewritten


def test_gp_rewrite_to_temp_tables_materializes_compound_cte_as_one_table() -> None:
    rewritten = sql_format.gp_rewrite_to_temp_tables(
        "with users as ("
        "select distinct contact_id from user_flags where mandatory_user_flg = 0"
        "), cheques as ("
        "select t1.contact_id, t1.cheque_pk from cheques_source as t1 "
        "where t1.operation_type_id = 1"
        "), articles_filter as ("
        "select article_id from supplier_codes "
        "union "
        "select t1.article_id from articles as t1 "
        "join promo_codes as t2 on t1.code = cast(t2.promo_code as text)"
        "), cheque_items as ("
        "select t2.contact_id, t1.article_id, sum(t1.amount) as volume "
        "from cheque_items_source as t1 "
        "join cheques as t2 on t1.cheque_pk = t2.cheque_pk "
        "join articles_filter as t3 on t1.article_id = t3.article_id "
        "group by t2.contact_id, t1.article_id"
        ") "
        "select contact_id, article_id, volume from cheque_items"
    )

    assert rewritten.count("create temporary table") == 4
    assert "create temporary table articles_filter as (" in rewritten
    assert "\n    union\n" in rewritten
    assert "create temporary table tmp_" not in rewritten


@pytest.mark.parametrize(
    ("sql", "temp_name", "final_reference"),
    [
        (
            "select * from (select id from first_source "
            "union select id from second_source) as combined",
            "combined",
            "from combined",
        ),
        (
            "select * from users where id in (select id from first_source "
            "union select id from second_source)",
            "tmp_1",
            "from tmp_1",
        ),
    ],
)
def test_gp_rewrite_to_temp_tables_materializes_compound_subquery_as_one_table(
    sql: str,
    temp_name: str,
    final_reference: str,
) -> None:
    rewritten = sql_format.gp_rewrite_to_temp_tables(sql)

    assert rewritten.count("create temporary table") == 1
    assert f"create temporary table {temp_name} as (" in rewritten
    assert "\n    union\n" in rewritten
    assert final_reference in rewritten.rsplit("analyze", maxsplit=1)[-1]


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


def test_gp_rewrite_to_temp_tables_preserves_parenthesized_set_operand() -> None:
    rewritten = sql_format.gp_rewrite_to_temp_tables(
        "with combined as (select id from first_source union "
        "(select id from second_source union select id from third_source)) "
        "select * from combined"
    )

    assert rewritten.count("create temporary table") == 1
    assert rewritten.count("union\n") == 2


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
    "sql",
    [
        "",
        "select 1; select 2",
        "select 1 union select 2",
        "delete from users",
        "select * from orders",
        "with c as (select id from orders), c as (select id from users) select * from c",
        "select * from (select id from orders) s join (select id from users) s on s.id = s.id",
        "select * from users where exists (select 1 from orders o where o.user_id = users.id)",
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


@pytest.mark.parametrize("operator", ["union all", "intersect", "except"])
def test_gp_rewrite_to_temp_tables_supports_nested_set_operations(
    operator: str,
) -> None:
    rewritten = sql_format.gp_rewrite_to_temp_tables(
        f"with combined as (select id from first_source {operator} "
        "select id from second_source) select * from combined"
    )

    assert rewritten.count("create temporary table") == 1
    assert "create temporary table combined as (" in rewritten
    assert f"\n    {operator}\n" in rewritten


def test_gp_rewrite_to_temp_tables_uppercase_keyword_case() -> None:
    from analytics_toolkit.sql_format import gp_rewrite_to_temp_tables

    assert (
        gp_rewrite_to_temp_tables(
            "select * from (select user_id from orders) s join users u on s.user_id = u.id",
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


def test_rewrite_with_ctes_exposes_cte_blank_lines() -> None:
    from analytics_toolkit.sql_format import rewrite_with_ctes

    assert (
        rewrite_with_ctes(
            "with old as (select 1 as x) select s.x from (select x from old) s",
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


def test_rewrite_with_ctes_rejects_nested_derived_tables() -> None:
    with pytest.raises(ValueError, match="nested derived subqueries"):
        sql_format.rewrite_with_ctes(
            "select * from (select * from (select id from events) inner_s) outer_s"
        )


def test_rewrite_with_ctes_rejects_non_select_statement() -> None:
    with pytest.raises(ValueError, match="expects a SELECT"):
        sql_format.rewrite_with_ctes("delete from events")


def test_rewrite_with_ctes_reports_injected_residual_select(
    monkeypatch,
) -> None:
    expression = parse_one("select * from (select id from events) s")
    monkeypatch.setattr(exp.Subquery, "replace", lambda self, expression: self)

    with pytest.raises(ValueError, match="confidently rewrite all SELECT subqueries"):
        sql_format._extract_supported_ctes(expression, cte_prefix="cte")


def test_rewrite_with_ctes_skips_existing_generated_name() -> None:
    rewritten = sql_format.rewrite_with_ctes(
        "with cte_1 as (select 1 as id) select s.id from (select id from cte_1) s"
    )

    assert "cte_1 as (" in rewritten
    assert "cte_2 as (" in rewritten
    assert "from cte_2 as s" in rewritten


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
