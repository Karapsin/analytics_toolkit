"""Static native functions use the selected session's actual namespace."""

from __future__ import annotations

from typing import Any

from analytics_toolkit import sql
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from sqlglot import exp, parse_one

from tests.sql._support.outage import Client, Result


def _runtime_database(outage: Any, monkeypatch: Any, *, configured: str | None) -> list[str]:
    cluster, configure = outage
    configure(driver="native", cluster_routing=None, database=configured)
    cluster.members = [(1, 1, "h1", 9000)]
    cluster.tables.clear()
    for database, value in [("db", 11), ("actual", 22)]:
        cluster.create(
            "h1", f"CREATE TABLE {database}.records (id UInt64) ENGINE=MergeTree ORDER BY id"
        )
        cluster.tables[("h1", f"{database}.records")].rows = [(value,)]
    probed: list[str] = []
    query_original, command_original = Client.query, Client.command

    def query(client: Any, statement: str, **kwargs: Any) -> Any:
        tree = parse_one(statement, read=ClickHouseDialect)
        if (
            isinstance(tree, exp.Select)
            and tree.args.get("from_") is None
            and isinstance(tree.expressions[0], exp.CurrentDatabase)
        ):
            database = getattr(client, "active_database", configured or "actual")
            probed.append(database)
            return Result([(database,)], ("currentDatabase()",), ("String",))
        return query_original(client, statement, **kwargs)

    def command(client: Any, statement: str, **kwargs: Any) -> Any:
        tree = parse_one(statement, read=ClickHouseDialect)
        if isinstance(tree, exp.Use):
            client.active_database = tree.this.name
            return None
        return command_original(client, statement, **kwargs)

    monkeypatch.setattr(Client, "query", query)
    monkeypatch.setattr(Client, "command", command)
    return probed


def test_native_two_argument_function_resolves_omitted_configured_database(
    outage: Any, monkeypatch: Any
) -> None:
    probed = _runtime_database(outage, monkeypatch, configured=None)
    result = sql.read("ch", "SELECT id FROM cluster('core','records')", retry_cnt=1)
    assert result.id.tolist() == [22]
    assert probed == ["actual"]


def test_native_function_namespace_cache_changes_only_after_acknowledged_use(
    outage: Any, monkeypatch: Any
) -> None:
    probed = _runtime_database(outage, monkeypatch, configured="db")
    result = sql.execute_read(
        "ch",
        "SELECT id FROM cluster('core','records') LIMIT 0; USE actual; "
        "SELECT id FROM cluster('core','records')",
        retry_cnt=1,
    )
    assert result.id.tolist() == [22]
    assert probed == ["db", "actual"]


def test_qualified_native_function_needs_no_current_database_probe(
    outage: Any, monkeypatch: Any
) -> None:
    probed = _runtime_database(outage, monkeypatch, configured=None)
    result = sql.read("ch", "SELECT id FROM cluster('core','actual.records')", retry_cnt=1)
    assert result.id.tolist() == [22]
    assert probed == []
