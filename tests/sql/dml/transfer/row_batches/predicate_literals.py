"""Executable placeholders and exact source-backend literals (audit 41-44)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pandas as pd
import pytest
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.transfer_stage import (
    collision_stage_suffix,
    match_transfer_stage_identifier,
)
from analytics_toolkit.sql.core.exact_values import ExactTemporal
from analytics_toolkit.sql.dml.transfer.flow.placeholders import replace_predicate_placeholders

from tests.sql._support.row_batches import keys_module


def test_empty_predicate_mapping_preserves_sql() -> None:
    assert replace_predicate_placeholders("SELECT '{}'", {}) == "SELECT '{}'"


@pytest.mark.parametrize(
    "protected",
    ["'{id}'", '"{id}"', "`{id}`", "[{id}]", "$$ {id} $$", "$body$ {id} $body$"],
)
def test_placeholder_preserves_strings_identifiers_and_dollar_bodies(protected: str) -> None:
    query = f"SELECT {protected} FROM t WHERE {{id}} -- {{id}}\n/* {{id}} */"
    result = keys_module.render_transfer_slice_source_sql(
        query, transfer_keys=[keys_module.TransferKey("id", "id")], values=(42,)
    )
    assert result == f"SELECT {protected} FROM t WHERE (id) = 42 -- {{id}}\n/* {{id}} */"


@pytest.mark.parametrize("query", ["SELECT '{id}'", 'SELECT "{id}"', "SELECT 1 -- {id}"])
def test_nonexecuting_placeholder_does_not_satisfy_key(query: str) -> None:
    with pytest.raises(ValueError, match=r"Missing placeholder: \{id\}"):
        keys_module.render_transfer_slice_source_sql(
            query, transfer_keys=[keys_module.TransferKey("id", "id")], values=(1,)
        )


def test_inserted_placeholder_text_is_never_scanned_again() -> None:
    result = keys_module.render_transfer_slice_source_sql(
        "SELECT * FROM t WHERE {a} OR {b} OR {a}",
        transfer_keys=[keys_module.TransferKey("a", "a"), keys_module.TransferKey("b", "b")],
        values=("{b}", 42),
    )
    assert result == "SELECT * FROM t WHERE (a) = '{b}' OR (b) = 42 OR (a) = '{b}'"


@pytest.mark.parametrize("comment", ["/* outer /* inner */ {id} */", "/* /* */ ' {id} */"])
def test_nested_comments_never_supply_an_executable_filter(comment: str) -> None:
    with pytest.raises(ValueError, match=r"Missing placeholder: \{id\}"):
        replace_predicate_placeholders(comment + " SELECT 1", {"id": "(id) = 42"})
    query = comment + " SELECT * FROM t WHERE {id}"
    assert replace_predicate_placeholders(query, {"id": "(id) = 42"}) == (
        comment + " SELECT * FROM t WHERE (id) = 42"
    )


def test_unterminated_comment_cannot_supply_a_filter() -> None:
    with pytest.raises(ValueError, match=r"Missing placeholder: \{id\}"):
        replace_predicate_placeholders("SELECT 1 /* {id}", {"id": "(id) = 42"})


def test_clickhouse_literals_escape_backslashes_and_preserve_precise_instants() -> None:
    ch = get_backend_adapter("ch")
    assert ch.render_transfer_literal(2**64 - 1) == "18446744073709551615"
    assert ch.render_transfer_literal("a\\'b") == "'a\\\\\\'b'"
    assert ch.render_transfer_literal(pd.Timestamp("2025-01-01 01:02:03.123456789")) == (
        "toDateTime64('2025-01-01 01:02:03.123456789', 9)"
    )
    aware = datetime(2025, 1, 1, 3, tzinfo=timezone(timedelta(hours=3)))
    assert ch.render_transfer_literal(aware) == "toDateTime64('2025-01-01 00:00:00', 0, 'UTC')"
    assert ch.render_transfer_literal(date(1900, 1, 1)) == "toDate32('1900-01-01')"


@pytest.mark.parametrize("naive_output", [False, True])
def test_exact_temporal_read_values_remain_usable_as_clickhouse_keys(naive_output: bool) -> None:
    stamp = ExactTemporal(103819392001234567, 7, "Europe/Moscow", naive_output)
    *_rest, slices, _concurrency = keys_module.normalize_transfer_slices(
        source_sql="SELECT * FROM t WHERE {k}",
        source_backend="ch",
        transfer_keys="k",
        transfer_key_values=[stamp],
        concurrency=1,
    )
    text = stamp.isoformat(utc=True).replace("T", " ", 1)[:-6]
    assert slices[0].predicate_sql == f"(k) = toDateTime64('{text}', 7, 'UTC')"
    assert get_backend_adapter("ch").render_transfer_literal(ExactTemporal(1234567, 7)) == (
        "toDateTime64('1970-01-01 00:00:00.1234567', 7)"
    )


def test_normalized_slices_use_source_backend_literals() -> None:
    *_rest, slices, _concurrency = keys_module.normalize_transfer_slices(
        source_sql="SELECT * FROM t WHERE {k}",
        source_backend="ch",
        transfer_keys="k",
        transfer_key_values=["a\\b'c"],
        concurrency=1,
    )
    assert slices[0].source_sql == "SELECT * FROM t WHERE (k) = 'a\\\\b\\'c'"


@pytest.mark.parametrize("collision", ["", "abcd", "abcde", "__c_abcdef01"])
def test_current_and_legacy_transfer_stage_collisions_are_recognized(collision: str) -> None:
    transfer_id = "a" * 32
    identifier = f"deadbeefdeadbeef__target__{transfer_id}__s00001{collision}"
    match = match_transfer_stage_identifier(identifier)
    assert match is not None
    assert match.group("transfer_id") == transfer_id


def test_generated_collision_suffix_is_recognized() -> None:
    suffix = collision_stage_suffix("gp", "a" * 32 + "__source", "0123456789abcdef")
    assert match_transfer_stage_identifier(suffix) is not None
