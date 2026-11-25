"""Actual routing/topology around a relational metadata server boundary."""

from __future__ import annotations

from typing import Any

from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from sqlglot import exp, parse_one

from tests.sql._support.ch_metadata_sqlite import MetadataServer
from tests.sql._support.outage import Client, Result
from tests.sql._support.outage_metadata import _table_definition


def install_statistics_boundary(monkeypatch: Any, cluster: Any) -> MetadataServer:
    tables = []
    for position, member in enumerate(cluster.members, 1):
        for (host, _name), table in cluster.tables.items():
            if host != member[2]:
                continue
            metadata = _table_definition(table)
            rows = len(table.rows) if table.engine != "Distributed" else None
            tables.append(
                (
                    position,
                    metadata["database"],
                    metadata["name"],
                    rows,
                    rows * 8 if rows is not None else None,
                    table.engine,
                    table.engine_full,
                )
            )
    server = MetadataServer([(member[0], member[1]) for member in cluster.members], tables)
    for position, member in enumerate(cluster.members, 1):
        if member[2] in cluster.replica_expired:
            server.connection.execute(
                "UPDATE host_replicas SET is_session_expired = 1 WHERE _shard_num = ?",
                (position,),
            )
    original = Client.query

    def query(client: Any, statement: str, **kwargs: Any) -> Any:
        tree = parse_one(statement, read=ClickHouseDialect)
        source = tree.args.get("from_")
        metadata = (
            source is not None
            and isinstance(source.this, exp.Subquery)
            and (source.this.alias == "table_metadata")
        )
        statistics = any(cte.alias == "host_scope" for cte in tree.find_all(exp.CTE))
        if not metadata and not statistics:
            return original(client, statement, **kwargs)
        frame = server.read("ch", statement)
        kinds = [
            "Nullable(UInt64)"
            if column in {"row_count", "table_size_bytes"}
            else "UInt8"
            if column == "statistics_complete"
            else "String"
            for column in frame.columns
        ]
        return Result(list(frame.itertuples(index=False, name=None)), list(frame.columns), kinds)

    monkeypatch.setattr(Client, "query", query)
    return server
