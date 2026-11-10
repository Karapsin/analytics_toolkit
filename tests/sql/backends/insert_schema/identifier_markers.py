from __future__ import annotations

from types import SimpleNamespace

import pytest
from analytics_toolkit.sql.backends import get_backend
from analytics_toolkit.sql.backends.trino import insert as trino_insert
from psycopg2.extras import _split_sql


def test_typed_trino_insert_does_not_rewrite_question_marks_in_identifiers_or_label():
    adapter = get_backend("trino")
    calls = []
    cursor = SimpleNamespace(
        execute=lambda sql, values: calls.append((sql, values)), close=lambda: None
    )
    connection = SimpleNamespace(cursor=lambda: cursor)
    trino_insert.insert_rows(
        adapter,
        connection,
        '"catalog?"."schema"."table?"',
        ["value?"],
        [(42,), (7,)],
        target_column_types={"value?": "BIGINT"},
        trino_insert_chunk_size=2,
        query_label="why?",
    )
    statement, values = calls[0]
    assert '"catalog?"."schema"."table?" ("value?")' in statement
    assert "VALUES (CAST(? AS BIGINT)), (CAST(? AS BIGINT))" in statement
    assert "query_label=why_" in statement
    assert values == [42, 7]


def test_compatibility_parameter_casts_only_replace_executable_tokens():
    statement = "INSERT INTO \"?\" SELECT ?, '?', $$?$$ /* ? */ -- ?\n"
    result = trino_insert.cast_parameter_placeholders(
        statement, ["id"], {"id": "BIGINT"}, row_count=1
    )
    assert result == "INSERT INTO \"?\" SELECT CAST(? AS BIGINT), '?', $$?$$ /* ? */ -- ?\n"
    with pytest.raises(ValueError, match="Missing executable"):
        trino_insert.cast_parameter_placeholders(
            "SELECT '?' /* ? */", ["id"], {"id": "BIGINT"}, row_count=1
        )


def test_greenplum_execute_values_template_escapes_identifier_and_label_percents():

    adapter = get_backend("gp")
    statement = adapter.build_dataframe_batch_insert_sql(
        '"data%"."target%s"', ["value%", "text%s"], row_count=1, query_label="50%"
    )
    before, after = _split_sql(statement.encode())
    prefix = b"".join(before).decode()
    suffix = b"".join(after).decode()
    assert '"data%"."target%s"' in prefix
    assert '"value%", "text%s"' in prefix
    assert "query_label=50_" in prefix + suffix
    assert statement.count(" VALUES %s") == 1


def test_legacy_placeholder_casting_preserves_sql_when_no_schema_is_resolved():
    statement = "INSERT INTO \"why?\" SELECT ?, '?' /* ? */"
    assert (
        trino_insert.cast_parameter_placeholders(statement, ["id"], None, row_count=1) == statement
    )
    assert trino_insert.cast_parameter_placeholders(statement, ["id"], {}, row_count=1) == statement


def test_legacy_placeholder_casting_rejects_extra_executable_parameters():
    with pytest.raises(ValueError, match="Too many executable parameter placeholders"):
        trino_insert.cast_parameter_placeholders(
            "SELECT ?, ?", ["id"], {"id": "BIGINT"}, row_count=1
        )


@pytest.mark.parametrize("row_count", [0, -1])
def test_typed_parameter_sql_rejects_nonpositive_row_counts_before_submission(row_count):
    with pytest.raises(ValueError, match="row_count must be a positive integer"):
        trino_insert.build_typed_insert_sql(
            get_backend("trino"), "sandbox.target", ["id"], {"id": "BIGINT"}, row_count=row_count
        )
