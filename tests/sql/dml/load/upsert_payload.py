"""Partition rebuilds retain full target values and recoverable native payloads."""

from __future__ import annotations

from copy import deepcopy

import pandas as pd
import pytest
from analytics_toolkit import general, sql

from tests.sql._support.trino_upsert_payload import INCOMING, SCHEMA, SEED, configure_upsert


def _load(*, requested=None, retry_cnt=1):
    return sql.load_df(
        "trino",
        "sandbox.events",
        pd.DataFrame([INCOMING]),
        table_schema=requested,
        write_mode="upsert",
        key_columns="id",
        upsert_partition_column="month",
        retry_cnt=retry_cnt,
        timeout_increment=0,
    )


@pytest.mark.parametrize("narrow_input", [False, True])
def test_public_trino_upsert_preserves_untouched_rows_at_full_target_precision(
    monkeypatch, write_sql_connections, narrow_input
):
    database, _other, _configuration = configure_upsert(monkeypatch, write_sql_connections)
    requested = {**SCHEMA, "value": "TIMESTAMP(3)"} if narrow_input else None

    result = _load(requested=requested)

    assert result == 1
    assert type(result) is int
    assert sorted(database.target.rows, key=lambda row: row["id"]) == [INCOMING, *SEED[1:]]
    assert database.target.schema == SCHEMA
    assert set(database.tables) == {("sandbox", "events")}
    assert len(database.created_schemas) == 2
    assert database.created_schemas[-1][1] == SCHEMA
    assert len(database.partition_drops) == 1
    assert database.formatted


@pytest.mark.parametrize("interruption", [KeyboardInterrupt(), SystemExit(17)])
def test_public_upsert_interruption_after_partition_drop_retains_the_prepared_payload(
    monkeypatch, write_sql_connections, interruption
):
    database, _other, _configuration = configure_upsert(monkeypatch, write_sql_connections)
    database.publication_error = interruption

    with pytest.raises(type(interruption)) as raised:
        _load()

    assert raised.value is interruption
    assert len(database.partition_drops) == 1
    assert database.target.rows == [SEED[2]]
    private = {
        name: table for name, table in database.tables.items() if name != ("sandbox", "events")
    }
    assert len(private) == 2
    assert any(
        sorted(table.rows, key=lambda row: row["id"]) == [INCOMING, SEED[1]]
        for table in private.values()
    )
    assert len(interruption.analytics_toolkit_sql_acknowledged_resources) == 2
    assert interruption.analytics_toolkit_sql_prepared_payload is not None
    assert all(connection.close_calls == 1 for connection in database.opened)


def test_public_prepared_trino_upsert_rejects_alias_retarget_before_replay_mutation(
    monkeypatch, write_sql_connections
):
    database, other, configuration = configure_upsert(monkeypatch, write_sql_connections)
    foreign = deepcopy(other.target.rows)
    failure = ConnectionError("The prepared insert connection was interrupted.")
    database.publication_error = failure

    def retarget():
        configuration["trino"]["host"] = "second.invalid"
        path = write_sql_connections(configuration)
        general.set_connections_path(path)

    database.publication_callback = retarget

    with pytest.raises(ValueError, match=r"[Cc]onfiguration|namespace|identity"):
        _load(retry_cnt=2)

    assert other.target.rows == foreign
    assert other.live_mutations == []
    assert database.target.rows == [SEED[2]]
    assert len(database.tables) == 3
    assert all(connection.close_calls == 1 for connection in [*database.opened, *other.opened])


def test_public_upsert_data_retry_reuses_the_prepared_payload_after_lost_ack(
    monkeypatch, write_sql_connections
):
    database, _other, _configuration = configure_upsert(monkeypatch, write_sql_connections)
    database.publication_error = ConnectionError("The INSERT committed but its ACK was lost.")
    database.fail_after_ack = True

    assert _load(retry_cnt=2) == 1

    assert sorted(database.target.rows, key=lambda row: row["id"]) == [INCOMING, *SEED[1:]]
    assert len(database.created_schemas) == 2
    assert len(database.publications) == 2
    assert len(database.partition_drops) == 2
    assert set(database.tables) == {("sandbox", "events")}
