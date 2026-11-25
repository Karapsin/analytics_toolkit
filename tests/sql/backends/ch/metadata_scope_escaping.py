"""Routed DESCRIBE uses ClickHouse literal rules for structured identities."""

from __future__ import annotations

from typing import Any

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends import utils
from analytics_toolkit.sql.backends.ch import outage_metadata
from analytics_toolkit.sql.backends.ch.identifiers import quote_identifier


@pytest.mark.parametrize(
    ("database", "relation"), [(r"db\namespace", "events"), ("db", r"events\name")]
)
@pytest.mark.parametrize("qualified", [False, True])
def test_public_table_info_keeps_backslash_identity_in_routed_describe(
    outage: Any, monkeypatch: Any, database: str, relation: str, qualified: bool
) -> None:
    cluster, configure = outage
    configure(database=database)
    cluster.tables.clear()
    reference = f"{quote_identifier(database)}.{quote_identifier(relation)}"
    target = reference if qualified else quote_identifier(relation)
    for host in ("h1", "h2"):
        cluster.create(
            host,
            f"CREATE TABLE {reference} (actual_column UInt64) ENGINE=MergeTree ORDER BY tuple()",
        )
    # Exercise the original faulty renderer through the same public path as a
    # bounded control; no mocked layout or resolver hides the lost identity.
    with monkeypatch.context() as legacy:
        legacy.setattr(outage_metadata, "sql_string_literal", utils.sql_string_literal)
        assert sql.table_info("ch", target).columns != {"actual_column": "UInt64"}
    assert sql.table_info("ch", target).columns == {"actual_column": "UInt64"}
