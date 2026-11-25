"""Configured DNS aliases cannot multiply one actual ClickHouse server."""

from __future__ import annotations

from typing import Any

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from sqlglot import exp, parse_one

from tests.sql._support.outage import Client, Result


def test_public_read_rejects_two_members_resolving_to_one_server_instance(
    outage: Any, monkeypatch: Any
) -> None:
    cluster, _configure = outage
    original = Client.query

    def query(client: Any, statement: str, **kwargs: Any) -> Any:
        result = original(client, statement, **kwargs)
        if "system, one" not in statement:
            return result
        tree = parse_one(statement, read=ClickHouseDialect)
        rows = []
        for row in result.result_rows:
            values = list(row)
            for index, expression in enumerate(tree.expressions):
                if any(
                    isinstance(node, exp.Anonymous) and node.name.lower() == "serveruuid"
                    for node in expression.walk()
                ):
                    values[index] = "12345678-1234-4234-9234-123456789abc"
            rows.append(tuple(values))
        return Result(rows, result.column_names, result.column_types)

    monkeypatch.setattr(Client, "query", query)
    with pytest.raises(sql.ClickHouseClusterTopologyError, match=r"instance|identity|unique"):
        sql.read("ch", "SELECT id FROM db.events", retry_cnt=1)
    assert not cluster.selected_reads
