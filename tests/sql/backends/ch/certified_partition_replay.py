"""Prepared upsert payloads replay data mutations without relaxing cutover fences."""

from __future__ import annotations

from typing import Any

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from analytics_toolkit.sql.dml.table.upsert_checkpoint import UpsertCheckpoint
from analytics_toolkit.sql.dml.table.write_modes import upsert_stage_table
from analytics_toolkit.sql.execution.backend_options import backend_operation
from sqlglot import exp, parse_one

from tests.sql._support.outage import Client


def _prepared_payload(outage: Any, monkeypatch: Any, *, driver: str) -> tuple[Any, list[str]]:
    cluster, configure = outage
    configure(driver=driver, cluster_routing=None)
    cluster.tables.clear()
    for host in ("h1", "h2"):
        cluster.create(
            host,
            "CREATE TABLE db.events (id Nullable(UInt64)) "
            "ENGINE=MergeTree PARTITION BY id ORDER BY tuple()",
        )
    cluster.create(
        "h1", "CREATE TABLE db.payload (id Nullable(UInt64)) ENGINE=MergeTree ORDER BY tuple()"
    )
    cluster.tables[("h1", "db.events")].rows = [(1,), (2,)]
    cluster.tables[("h2", "db.events")].rows = [(99,)]
    cluster.tables[("h1", "db.payload")].rows = [(1,)]
    attempts: list[str] = []
    original = Client.command

    def command(client: Any, statement: str, **kwargs: Any) -> Any:
        tree = parse_one(statement, read=ClickHouseDialect)
        result = original(client, statement, **kwargs)
        if isinstance(tree, exp.Alter) and tree.find(exp.DropPartition):
            target = tree.this
            identity = (client.host, f"{target.db or 'db'}.{target.name}")
            cluster.tables[identity].rows = [
                row for row in cluster.tables[identity].rows if row[0] != 1
            ]
            attempts.append(statement)
            if len(attempts) == 1:
                message = "partition acknowledgement lost"
                raise ConnectionError(message)
        return result

    monkeypatch.setattr(Client, "command", command)
    return cluster, attempts


@pytest.mark.parametrize("driver", ["http", "native"])
def test_actual_outage_client_replays_only_the_prepared_partition_payload(
    outage: Any, monkeypatch: Any, driver: str
) -> None:
    cluster, attempts = _prepared_payload(outage, monkeypatch, driver=driver)
    checkpoint = UpsertCheckpoint(
        final_stage_table="db.payload",
        target_table="db.events",
        backend="ch",
        replay_sqls=[
            "ALTER TABLE db.events DROP PARTITION 1",
            "INSERT INTO db.events (id) SELECT id FROM db.payload",
        ],
        prepared=True,
    )
    connection = sql.get_sql_connection("ch")

    def finalize() -> None:
        upsert_stage_table(
            "ch",
            connection,
            "db.events",
            "db.incoming",
            columns=["id"],
            key_columns=["id"],
            column_types={"id": "Nullable(UInt64)"},
            upsert_partition_column="id",
            final_stage_table="db.payload",
            checkpoint=checkpoint,
        )

    try:
        with backend_operation() as state:
            with pytest.raises(ConnectionError, match="acknowledgement lost"):
                finalize()
            assert not state.structural_replay_blocked
            assert checkpoint.prepared
            assert checkpoint.mutation_started
            assert cluster.tables[("h1", "db.payload")].rows == [(1,)]
            finalize()
        assert len(attempts) == 2
        assert sorted(cluster.tables[("h1", "db.events")].rows) == [(1,), (2,)]
        assert cluster.tables[("h2", "db.events")].rows == [(99,)]
    finally:
        connection.close()


def test_partition_alter_without_a_certified_payload_keeps_structural_recovery_fence(
    outage: Any, monkeypatch: Any
) -> None:
    cluster, _attempts = _prepared_payload(outage, monkeypatch, driver="http")
    connection = sql.get_sql_connection("ch")
    try:
        with backend_operation() as state:
            with pytest.raises(sql.AmbiguousSqlReplaceError):
                connection.command("ALTER TABLE db.events DROP PARTITION 1")
            assert state.structural_replay_blocked
            assert state.preserve_recovery
        assert cluster.tables[("h1", "db.payload")].rows == [(1,)]
        assert cluster.tables[("h2", "db.events")].rows == [(99,)]
    finally:
        connection.close()
