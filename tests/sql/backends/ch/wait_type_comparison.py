"""Readiness compares native type structure without erasing string contents."""

from analytics_toolkit.sql.backends.ch.wait import (
    _build_ch_expected_schema_condition,
    _normalize_ch_type_for_comparison,
)


def test_ch_wait_type_comparison_recognizes_sql_integer_aliases() -> None:
    assert _normalize_ch_type_for_comparison("BIGINT") == _normalize_ch_type_for_comparison("Int64")


def test_ch_wait_type_comparison_preserves_enum_label_whitespace() -> None:
    assert _normalize_ch_type_for_comparison(
        "Enum8('a b' = 1)"
    ) != _normalize_ch_type_for_comparison("Enum8('ab' = 1)")


def test_ch_wait_type_comparison_queries_canonical_types_without_erasing_literals() -> None:
    condition = _build_ch_expected_schema_condition(
        {"id": "BIGINT", "kind": "Enum8('a b' = 1)", "payload": "Object('json')"}
    )
    assert "type = 'Int64'" in condition
    assert "a b" in condition
    assert "Object(" in condition
    assert "replaceRegexpAll" not in condition


def test_ch_wait_type_comparison_preserves_clickhouse_date_spelling_recursively() -> None:
    assert _normalize_ch_type_for_comparison("DATE") == "Date"
    assert _normalize_ch_type_for_comparison("Array(Nullable(DATE))") == "Array(Nullable(Date))"
    condition = _build_ch_expected_schema_condition({"day": "DATE"})
    assert "type = 'Date'" in condition


def test_ch_wait_type_comparison_keeps_unrecognized_diagnostic_text_exact() -> None:
    assert _normalize_ch_type_for_comparison("Wrong") == "Wrong"
    assert _normalize_ch_type_for_comparison("Wrong('a b')") == "Wrong('a b')"
