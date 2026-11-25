"""Legacy raw arguments remain distinct from certified static engine identity."""

from __future__ import annotations

import pytest
from analytics_toolkit.sql.backends.ch.metadata import (
    extract_clickhouse_distributed_shard_table,
    normalize_clickhouse_distributed_database_arg,
    normalize_clickhouse_engine_arg,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("'not'quoted'", "not'quoted"),
        ('"not"quoted"', 'not"quoted'),
        ("`not`quoted`", "not`quoted"),
        ("(abc(", "(abc("),
    ],
)
def test_metadata_compatibility_retains_best_effort_malformed_raw_arguments(raw, expected):
    assert normalize_clickhouse_engine_arg(raw) == expected
    assert normalize_clickhouse_distributed_database_arg(raw, "current") == expected
    assert (
        extract_clickhouse_distributed_shard_table(
            f"Distributed('core', {raw}, 'events_shard')", "current"
        )
        is None
    )


@pytest.mark.parametrize("raw", ["1; SELECT 2", "currentDatabase(); SELECT 'other'"])
def test_metadata_compatibility_never_certifies_executable_statement_sequences(raw):
    # The raw compatibility helper returns text; it never confers static ownership.
    assert normalize_clickhouse_engine_arg(raw) == raw
    assert normalize_clickhouse_distributed_database_arg(raw, "current") == raw
    assert (
        extract_clickhouse_distributed_shard_table(
            f"Distributed('core', {raw}, 'events_shard')", "current"
        )
        is None
    )


def test_metadata_compatibility_unterminated_database_call_does_not_select_current_namespace():
    raw = "database('unterminated)"

    assert normalize_clickhouse_distributed_database_arg(raw, "current") == raw
    assert (
        extract_clickhouse_distributed_shard_table(
            f"Distributed('core', {raw}, 'events_shard')", "current"
        )
        is None
    )


@pytest.mark.parametrize(
    "engine",
    [
        "Distributed('core', 'db', 'unterminated)",
        "Distributed('core', 'db', 'events_shard', tuple(1)",
    ],
    ids=["unterminated-literal", "truncated-nested-expression"],
)
def test_metadata_compatibility_rejects_incomplete_static_engine_metadata(engine):
    assert extract_clickhouse_distributed_shard_table(engine, "current") is None
