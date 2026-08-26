from __future__ import annotations

from tests.sql_format._support.formatting import (
    exp,
    parse_one,
    pytest,
    sql_format,
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
    "helper_name",
    ["format_sql", "rewrite_with_ctes", "gp_rewrite_to_temp_tables"],
)
def test_sql_format_helpers_reject_non_string_sql(helper_name: str) -> None:
    with pytest.raises(ValueError, match="sql to be a string"):
        getattr(sql_format, helper_name)(None)


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


def test_sql_format_internal_join_layout_edges() -> None:
    assert sql_format._normalize_join_condition_layout("ON a.id = b.id") == ("ON a.id = b.id")
    assert sql_format._normalize_join_condition_layout("AND a.id = b.id") == ("AND a.id = b.id")
    assert (
        sql_format._normalize_join_condition_layout("where true\nAND a.id = b.id")
        == "where true\nAND a.id = b.id"
    )
    assert sql_format._split_join_condition_line("ON") == ["ON"]
    assert sql_format._split_join_condition_line("ON ") == ["ON "]
    assert sql_format._previous_non_empty_line(["one", "", ""], 3) == "one"


def test_sql_format_internal_layout_edges() -> None:
    assert (
        sql_format._compact_targeted_clause_layout(
            "GROUP BY\nnext",
            [sql_format._ClauseCompactionTarget("GROUP BY", "1")],
        )
        == "GROUP BY\nnext"
    )
    assert (
        sql_format._compact_targeted_clause_layout(
            "GROUP BY\n    2",
            [sql_format._ClauseCompactionTarget("GROUP BY", "1")],
        )
        == "GROUP BY\n    2"
    )
    assert sql_format._compact_clause_item_lines(["a +", "b,", ", c", "  + d"]) == "a + b, c + d"
    assert (
        sql_format._normalize_union_separator_layout(
            "select 1\n\n\nunion\nselect 2",
            1,
        )
        == "select 1\n\nunion\nselect 2"
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
        parse_one("select * from tmp join other on other.id = tmp.id and other.id = tmp.id"),
    ]
    assert (
        planner._distributed_columns(
            "missing",
            consumer_expressions=consumers,
        )
        == []
    )
    assert planner._distributed_columns(
        "tmp",
        consumer_expressions=consumers,
    ) == ["id"]

    joined = parse_one("select * from tmp join other on tmp.id = other.id")
    assert sql_format._local_select_source_names(joined) == {"tmp", "other"}


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
    assert (
        sql_format._join_column_for_temp(
            both_temp,
            consumer_names={"tmp"},
            dialect=None,
        )
        is None
    )
    assert (
        sql_format._join_column_for_temp(
            neither_temp,
            consumer_names={"tmp"},
            dialect=None,
        )
        is None
    )
    assert (
        sql_format._column_name_for_temp(
            exp.Literal.number(1),
            consumer_names={"tmp"},
            dialect=None,
        )
        is None
    )


def test_sql_format_internal_right_join_column_and_indentation_edges() -> None:
    right_temp = exp.EQ(
        this=exp.column("left_id", table="other"),
        expression=exp.column("right_id", table="tmp"),
    )
    assert (
        sql_format._join_column_for_temp(
            right_temp,
            consumer_names={"tmp"},
            dialect=None,
        )
        == "right_id"
    )

    assert (
        sql_format._normalize_leading_comma_indentation(
            "SELECT\n        a",
            4,
        )
        == "SELECT\n    a"
    )
    assert (
        sql_format._normalize_leading_comma_indentation(
            "SELECT\n    , a",
            4,
        )
        == "SELECT\n    , a"
    )
    assert (
        sql_format._normalize_join_condition_layout(
            "join b\n  ON a.id = b.id\n    AND a.kind = b.kind"
        )
        == "join b\n  ON a.id = b.id\n AND a.kind = b.kind"
    )


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


def test_sql_format_internal_where_and_quote_layout_edges() -> None:
    assert (
        sql_format._normalize_where_anchor_layout(
            "WHERE\n    x = 1",
            "1=1",
        )
        == "WHERE\n    x = 1"
    )
    assert sql_format._split_anchor_and_conditions("1 = 1", "1 = 1") == []
    assert sql_format._split_anchor_and_conditions(
        "1 = 1 OR x = 1",
        "1 = 1",
    ) == ["OR x = 1"]
    assert sql_format._split_top_level_and_conditions(
        "x = 'it''s AND ok' AND y = \"A AND B\" AND z = `C AND D` AND q = [E AND F]"
    ) == [
        "x = 'it''s AND ok'",
        'y = "A AND B"',
        "z = `C AND D`",
        "q = [E AND F]",
    ]


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


def test_sql_format_is_importable_from_root_package() -> None:
    from analytics_toolkit import sql_format

    assert sql_format.format_sql("select 1") == "select\n    1"


def test_sql_format_local_sources_support_select_without_from() -> None:
    assert sql_format._local_select_source_names(parse_one("select 1")) == set()


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
