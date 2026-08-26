from __future__ import annotations

from tests.sql_format._support.formatting import (
    SqlglotError,
    exp,
    pytest,
    sql_format,
)


def test_format_sql_adds_default_blank_line_before_unions() -> None:
    from analytics_toolkit.sql_format import format_sql

    assert (
        format_sql("select * from a union all select * from b union select * from c")
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


def test_format_sql_adds_default_where_anchor() -> None:
    from analytics_toolkit.sql_format import format_sql

    assert format_sql("select * from t where x=1") == "select *\nfrom t\nwhere 1=1\n      and x = 1"


def test_format_sql_aligns_join_condition_with_join_keyword() -> None:
    from analytics_toolkit.sql_format import format_sql

    assert (
        format_sql("select * from a join b on a.id = b.id and a.kind = b.kind") == "select *\n"
        "from a\n"
        "join b\n"
        "  on a.id = b.id\n"
        " and a.kind = b.kind"
    )


def test_format_sql_aligns_multiple_and_conditions_under_anchor() -> None:
    from analytics_toolkit.sql_format import format_sql

    assert (
        format_sql("select * from t where x=1 and y=2 and label = 'A AND B'") == "select *\n"
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
            "SELECT *\nFROM t\nWHERE 1=1\n      AND x = 1",
        ),
        (
            "lower",
            "select *\nfrom t\nwhere 1=1\n      and x = 1",
        ),
        (
            "capitalize",
            "Select *\nFrom t\nWhere 1=1\n      And x = 1",
        ),
    ],
)
def test_format_sql_applies_keyword_case_to_aligned_where_anchor(
    keyword_case: str,
    expected_sql: str,
) -> None:
    from analytics_toolkit.sql_format import format_sql

    assert format_sql("select * from t where x=1", keyword_case=keyword_case) == expected_sql


def test_format_sql_basic_pretty_output() -> None:
    from analytics_toolkit.sql_format import format_sql

    assert (
        format_sql("select a,b from t order by b desc") == "select\n"
        "    a,\n"
        "    b\n"
        "from t\n"
        "order by 2 desc"
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


def test_format_sql_can_disable_blank_lines_between_ctes() -> None:
    from analytics_toolkit.sql_format import format_sql

    assert (
        format_sql(
            "with a as (select 1 as x), b as (select x from a) select * from b",
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


def test_format_sql_compacts_single_star_select_lists() -> None:
    from analytics_toolkit.sql_format import format_sql

    assert format_sql("select * from t") == "select *\nfrom t"
    assert format_sql("select t.* from t") == "select t.*\nfrom t"
    assert format_sql("select distinct * from t") == "select distinct *\nfrom t"
    assert format_sql("select distinct t.* from t") == "select distinct t.*\nfrom t"


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


def test_format_sql_defaults_group_by_to_ordinals() -> None:
    from analytics_toolkit.sql_format import format_sql

    assert (
        format_sql("select a,b,count(*) from t group by a,b") == "select\n"
        "    a,\n"
        "    b,\n"
        "    COUNT(*)\n"
        "from t\n"
        "group by 1, 2"
    )


def test_format_sql_defaults_order_by_to_ordinals_with_direction() -> None:
    from analytics_toolkit.sql_format import format_sql

    assert (
        format_sql("select a,b from t order by a,b desc") == "select\n"
        "    a,\n"
        "    b\n"
        "from t\n"
        "order by 1, 2 desc"
    )


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
            ("select countIf(x > 0) from events final where toDate(ts) = toDate('2026-06-01')"),
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


def test_format_sql_keeps_existing_group_and_order_ordinals() -> None:
    from analytics_toolkit.sql_format import format_sql

    assert (
        format_sql("select a,b from t group by 1 order by 2 desc") == "select\n"
        "    a,\n"
        "    b\n"
        "from t\n"
        "group by 1\n"
        "order by 2 desc"
    )


def test_format_sql_keeps_multi_star_select_lists_expanded() -> None:
    from analytics_toolkit.sql_format import format_sql

    assert (
        format_sql("select a.*, b.* from a join b on a.id = b.id") == "select\n"
        "    a.*,\n"
        "    b.*\n"
        "from a\n"
        "join b\n"
        "  on a.id = b.id"
    )
    assert format_sql("select *, x from t") == "select\n    *,\n    x\nfrom t"
    assert format_sql("select count(*) from t") == "select\n    COUNT(*)\nfrom t"


def test_format_sql_matches_group_by_alias_and_order_by_expression() -> None:
    from analytics_toolkit.sql_format import format_sql

    assert (
        format_sql("select a + 1 as day, count(*) as n from t group by day order by a + 1")
        == "select\n"
        "    a + 1 as day,\n"
        "    COUNT(*) as n\n"
        "from t\n"
        "group by 1\n"
        "order by 1"
    )


def test_format_sql_matches_group_by_expression_and_order_by_alias() -> None:
    from analytics_toolkit.sql_format import format_sql

    assert (
        format_sql("select a + 1 as day, sum(x) as sx from t group by a + 1 order by sx desc")
        == "select\n"
        "    a + 1 as day,\n"
        "    SUM(x) as sx\n"
        "from t\n"
        "group by 1\n"
        "order by 2 desc"
    )


def test_format_sql_mixes_expression_grouping_with_ordinal_ordering() -> None:
    formatted = sql_format.format_sql(
        "select a, b from t group by a, b order by b",
        group_by_format="expressions",
    )

    assert "group by\n    a,\n    b" in formatted
    assert formatted.endswith("order by 2")


def test_format_sql_ordinal_mapping_keeps_first_duplicate_projection() -> None:
    formatted = sql_format.format_sql(
        "select account_id, account_id from payments group by account_id"
    )

    assert formatted.endswith("group by 1")


def test_format_sql_preserves_group_by_expressions_with_star_projection() -> None:
    from analytics_toolkit.sql_format import format_sql

    assert format_sql("select * from t group by a, b, c") == "select *\nfrom t\ngroup by a, b, c"


def test_format_sql_preserves_semicolon_with_aligned_where_anchor() -> None:
    from analytics_toolkit.sql_format import format_sql

    assert (
        format_sql("select * from t where x=1;") == "select *\nfrom t\nwhere 1=1\n      and x = 1;"
    )


def test_format_sql_preserves_unmatched_group_and_order_expressions() -> None:
    from analytics_toolkit.sql_format import format_sql

    assert (
        format_sql("select a from t group by b order by c desc") == "select\n"
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


def test_format_sql_supports_trailing_and_leading_commas() -> None:
    from analytics_toolkit.sql_format import format_sql

    assert format_sql("select a,b from t", leading_commas=False) == "select\n    a,\n    b\nfrom t"
    assert format_sql("select a,b from t", leading_commas=True) == "select\n    a\n    , b\nfrom t"


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
        "with grouped as (select a from t group by a) select a from grouped group by a",
        order_by_format="expressions",
    )
    assert group_only.count("group by 1") == 2


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


def test_sql_format_clause_mapping_handles_duplicate_aliases() -> None:
    assert (
        sql_format.format_sql("select a as x, b as x from t order by x")
        == "select\n    a as x,\n    b as x\nfrom t\norder by x"
    )


def test_sql_format_compaction_stops_at_blank_line_and_handles_final_comma() -> None:
    target = sql_format._ClauseCompactionTarget("GROUP BY", "1")
    assert (
        sql_format._compact_targeted_clause_layout(
            "GROUP BY\n\n    1",
            [target],
        )
        == "GROUP BY\n\n    1"
    )
    assert sql_format._compact_clause_item_lines(["1,"]) == "1"


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
        lambda *args, **kwargs: (_ for _ in ()).throw(SqlglotError("distribution failed")),
    )
    with pytest.raises(ValueError, match="render distribution column"):
        sql_format._column_name_for_temp(
            column,
            consumer_names={"tmp"},
            dialect=None,
        )
