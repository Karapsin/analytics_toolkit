from __future__ import annotations

import analytics_toolkit.sql_format as formatter
import pytest
import sqlglot
from analytics_toolkit.sql_format import format_sql


@pytest.mark.parametrize(
    "source",
    [
        "with a as (select 1 as x) select * from a",
        "with a as (select 1 as x), b as (select x from a) select * from b",
        "with a as (with b as (select 1 as x) select * from b) select * from a",
        "with recursive a(x) as (select 1 union all select x+1 from a where x<5) select * from a",
        "with a as (select /* inside */ 1 as x), -- separator\n"
        "b as (select x from a) select * from b",
        "with a as (select (((((1 + 2))))) as x) select * from a",
        "with a as (select '), b AS (' as x) select * from a",
    ],
)
@pytest.mark.parametrize("dialect", ["postgres", "trino", "clickhouse"])
def test_cte_formatting_preserves_structure(source: str, dialect: str) -> None:
    formatted = format_sql(
        source,
        dialect=dialect,
        where_anchor="preserve",
        group_by_format="expressions",
        order_by_format="expressions",
    )
    assert sqlglot.parse_one(formatted, read=dialect) == sqlglot.parse_one(source, read=dialect)
    assert (
        format_sql(
            formatted,
            dialect=dialect,
            where_anchor="preserve",
            group_by_format="expressions",
            order_by_format="expressions",
        )
        == formatted
    )


@pytest.mark.parametrize(
    "suffix", ["; -- tail", ";\n-- tail", "\n-- tail", " /* tail */", "; /* tail */"]
)
def test_formatter_preserves_trailing_comments(suffix: str) -> None:
    formatted = format_sql("with a as (select 1) select * from a" + suffix)
    assert "tail" in formatted
    assert sqlglot.parse_one(formatted)


def test_deep_cte_and_quoted_aliases_remain_semantically_distinct() -> None:
    source = "with a as (select " + "(" * 40 + "1" + ")" * 40 + " as x) select * from a"
    assert sqlglot.parse_one(format_sql(source)) == sqlglot.parse_one(source)
    formatted = format_sql('select a as "Foo" from t order by "foo"')
    assert '"foo"' in formatted
    assert not formatted.endswith("order by 1")


@pytest.mark.parametrize("broken", ["select 2", "select ("])
def test_layout_corruption_falls_back_to_generator(
    monkeypatch: pytest.MonkeyPatch, broken: str
) -> None:
    monkeypatch.setattr(formatter, "_normalize_cte_separator_layout", lambda *_args: broken)
    assert sqlglot.parse_one(format_sql("select 1")) == sqlglot.parse_one("select 1")


def test_keyword_corruption_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(formatter, "_apply_keyword_case", lambda *_args, **_kwargs: "select 2")
    with pytest.raises(ValueError, match="preserve SQL semantics"):
        format_sql("select 1")
