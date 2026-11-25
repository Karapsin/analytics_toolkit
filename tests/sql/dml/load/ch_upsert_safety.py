"""Partition rebuilds need complete physical storage and one frozen authority."""

from __future__ import annotations

import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from analytics_toolkit.sql.backends.ch.outage_client import OutageClient
from analytics_toolkit.sql.connection.errors import InvalidSqlInputError
from sqlglot import exp, parse_one

from tests.sql._support.assertions import strip_query_label
from tests.sql._support.ch_upsert_safety import (
    COLUMNS,
    DATE_COLUMNS,
    DATE_EXPECTED,
    DATE_INCOMING,
    EXPECTED,
    INCOMING,
    PHYSICAL,
    TARGET,
    UpsertScenario,
    install_upsert_safety,
)
from tests.sql._support.reconfigure_conversion_scope import _name, _scope_sql


def _upsert(frame=None, *, partition_column="month", table_schema=None):
    frame = pd.DataFrame(INCOMING, columns=COLUMNS) if frame is None else frame
    original = frame.copy(deep=True)
    result = sql.load_df(
        "ch",
        TARGET,
        frame,
        table_schema=table_schema or dict.fromkeys(COLUMNS, "Int64"),
        write_mode="upsert",
        key_columns="id",
        upsert_partition_column=partition_column,
        retry_cnt=1,
        timeout_increment=0,
    )
    pd.testing.assert_frame_equal(frame, original)
    return result


def _attempt_upsert():
    try:
        _upsert()
    except (InvalidSqlInputError, ValueError) as error:
        return error
    return None


def _assert_rejected_before_ddl(state, before, error, words):
    after = state.snapshot()
    assert after == before, repr({"before": before, "after": after, "commands": state.commands})
    assert not state.commands, repr(state.commands)
    assert not state.created
    assert isinstance(error, (InvalidSqlInputError, ValueError)), repr(error)
    assert any(word in str(error).lower() for word in words), str(error)
    assert state.wrappers
    assert all(isinstance(client, OutageClient) for client in state.wrappers)


@pytest.mark.parametrize("driver", ["http", "native"])
@pytest.mark.parametrize("local", [True, False], ids=["local", "distributed"])
def test_public_upsert_simple_physical_partition_keeps_untouched_rows_and_identity(
    monkeypatch, write_sql_connections, driver, local
):
    state = install_upsert_safety(
        monkeypatch, write_sql_connections, scenario=UpsertScenario(driver=driver, local=local)
    )
    before = state.snapshot()

    assert _upsert() == len(INCOMING)

    after = state.snapshot()
    host, name = (state.coordinator, TARGET) if local else (state.remote, PHYSICAL)
    assert sorted(after[host][name]["rows"]) == sorted(EXPECTED)
    assert after[host][name]["uuid"] == before[host][name]["uuid"]
    assert after[host][name]["ddl"] == before[host][name]["ddl"]
    assert {endpoint: set(tables) for endpoint, tables in after.items()} == {
        endpoint: set(tables) for endpoint, tables in before.items()
    }
    if not local:
        assert after[state.coordinator][TARGET] == before[state.coordinator][TARGET]
    assert state.wrappers
    assert all(isinstance(client, OutageClient) for client in state.wrappers)
    assert state.opened
    assert all(connection.close_calls == 1 for connection in state.opened)


@pytest.mark.parametrize("driver", ["http", "native"])
def test_public_upsert_rebuilds_the_complete_modulo_partition_including_colliding_months(
    monkeypatch, write_sql_connections, driver
):
    state = install_upsert_safety(
        monkeypatch,
        write_sql_connections,
        scenario=UpsertScenario(driver=driver, local=True, partition="month % 10"),
    )
    before = state.snapshot()
    assert before[state.coordinator][TARGET]["rows"] == [(1, 1, 10), (11, 2, 20), (2, 3, 30)]

    assert _upsert() == len(INCOMING)

    after = state.snapshot()[state.coordinator][TARGET]
    assert sorted(after["rows"]) == [(1, 1, 11), (2, 3, 30), (11, 2, 20)]
    assert after["uuid"] == before[state.coordinator][TARGET]["uuid"]
    assert after["ddl"] == before[state.coordinator][TARGET]["ddl"]


@pytest.mark.parametrize("driver", ["http", "native"])
def test_public_upsert_evaluates_native_month_expression_on_target_typed_incoming_dates(
    monkeypatch, write_sql_connections, driver
):
    state = install_upsert_safety(
        monkeypatch,
        write_sql_connections,
        scenario=UpsertScenario(
            driver=driver,
            local=True,
            partition="toYYYYMM(date)",
            date_partition=True,
        ),
    )
    before = state.snapshot()
    frame = pd.DataFrame(DATE_INCOMING, columns=DATE_COLUMNS)

    assert _upsert(
        frame,
        partition_column="date",
        table_schema={"date": "Date", "id": "Int64", "value": "Int64"},
    ) == len(DATE_INCOMING)

    after = state.snapshot()[state.coordinator][TARGET]
    assert sorted(after["rows"]) == sorted(DATE_EXPECTED)
    assert after["uuid"] == before[state.coordinator][TARGET]["uuid"]
    assert after["ddl"] == before[state.coordinator][TARGET]["ddl"]


@pytest.mark.parametrize("driver", ["http", "native"])
def test_public_upsert_native_distributed_read_cannot_authorize_dropping_independent_replicas(
    monkeypatch, write_sql_connections, driver
):
    state = install_upsert_safety(
        monkeypatch, write_sql_connections, scenario=UpsertScenario(driver=driver, replicas=True)
    )
    before = state.snapshot()
    assert before[state.remote][PHYSICAL]["rows"] == [(1, 1, 10)]
    assert before["replica"][PHYSICAL]["rows"] == [(1, 2, 20)]

    error = _attempt_upsert()

    _assert_rejected_before_ddl(state, before, error, ("replica", "coverage", "topology"))


@pytest.mark.parametrize("driver", ["http", "native"])
@pytest.mark.parametrize(
    "lagging", [True, False], ids=["pending-acknowledged-block", "synchronous"]
)
def test_public_upsert_synchronizes_replicas_before_preserving_the_complete_partition(
    monkeypatch, write_sql_connections, driver, lagging
):
    state = install_upsert_safety(
        monkeypatch,
        write_sql_connections,
        scenario=UpsertScenario(
            driver=driver,
            replicas=True,
            replicated=True,
            replication_lag=lagging,
        ),
    )
    before = state.snapshot()
    # Both live Keeper leases are healthy. The first replica has not received
    # the acknowledged id=2 block that is present on its peer.
    if lagging:
        assert before[state.remote][PHYSICAL]["rows"] == [(1, 1, 10), (2, 3, 30)]
        assert state.pending_replication[state.remote, PHYSICAL] == [(1, 2, 20)]
    else:
        assert before[state.remote][PHYSICAL]["rows"] == before["replica"][PHYSICAL]["rows"]
        assert not state.pending_replication

    assert _upsert() == len(INCOMING)

    after = state.snapshot()
    for host in (state.remote, "replica"):
        assert sorted(after[host][PHYSICAL]["rows"]) == sorted(EXPECTED)
        assert after[host][PHYSICAL]["uuid"] == before[host][PHYSICAL]["uuid"]
        assert after[host][PHYSICAL]["ddl"] == before[host][PHYSICAL]["ddl"]
    assert not state.pending_replication
    assert {(host, table) for host, table, _order in state.sync_events} == {
        (state.remote, PHYSICAL),
        ("replica", PHYSICAL),
    }
    assert state.preservation_events
    assert max(order for _host, _table, order in state.sync_events) < min(state.preservation_events)


@pytest.mark.parametrize("driver", ["http", "native"])
def test_public_upsert_rejects_facade_retarget_after_payload_before_either_storage_is_mutated(
    monkeypatch, write_sql_connections, driver
):
    state = install_upsert_safety(
        monkeypatch, write_sql_connections, scenario=UpsertScenario(driver=driver, retarget=True)
    )
    before = state.snapshot()

    error = _attempt_upsert()

    assert state.retargeted, repr(state.commands)
    after = state.snapshot()
    for host in (state.remote, "replica"):
        assert after[host][PHYSICAL] == before[host][PHYSICAL], repr(
            {
                "before": before,
                "after": after,
                "commands": state.commands,
            }
        )
    assert after[state.coordinator][TARGET] == state.changed_snapshot[state.coordinator][TARGET]
    assert not any(
        strip_query_label(statement).lstrip().upper().startswith("ALTER TABLE")
        for statement in state.commands
    ), repr(state.commands)
    assert isinstance(error, (InvalidSqlInputError, ValueError)), repr(error)
    assert any(word in str(error).lower() for word in ("identity", "changed", "storage", "layout"))


@pytest.mark.parametrize("driver", ["http", "native"])
def test_public_upsert_facade_schema_cannot_authorize_rebuilding_hidden_physical_columns(
    monkeypatch, write_sql_connections, driver
):
    state = install_upsert_safety(
        monkeypatch,
        write_sql_connections,
        scenario=UpsertScenario(driver=driver, external_columns=True),
    )
    before = state.snapshot()
    physical = before[state.remote]["analytics.external_storage"]
    assert physical["rows"] == [
        (1, 1, 10, "private-1"),
        (1, 2, 20, "private-2"),
        (2, 3, 30, "private-3"),
    ]

    error = _attempt_upsert()

    _assert_rejected_before_ddl(state, before, error, ("column", "schema", "managed", "ownership"))


@pytest.mark.parametrize("driver", ["http", "native"])
@pytest.mark.parametrize("local", [True, False], ids=["local", "distributed"])
def test_public_upsert_explicit_narrow_input_keeps_full_native_preserved_values(
    monkeypatch, write_sql_connections, driver, local
):
    state = install_upsert_safety(
        monkeypatch,
        write_sql_connections,
        scenario=UpsertScenario(driver=driver, local=local, wide_value=True),
    )
    before = state.snapshot()
    frame = pd.DataFrame([(1, 1, 1)], columns=COLUMNS)

    assert _upsert(frame, table_schema={"month": "Int64", "id": "Int64", "value": "UInt8"}) == 1

    host, name = (state.coordinator, TARGET) if local else (state.remote, PHYSICAL)
    after = state.snapshot()[host][name]
    assert sorted(after["rows"]) == [(1, 1, 1), (1, 2, 1000), (2, 3, 30)]
    assert after["uuid"] == before[host][name]["uuid"]
    assert after["ddl"] == before[host][name]["ddl"]
    payload_creates = []
    incoming_value_casts = []
    for statement in state.commands:
        _cluster, body = _scope_sql(strip_query_label(statement))
        if body.lstrip().upper().startswith("CREATE TABLE"):
            tree = parse_one(body, read=ClickHouseDialect)
            if _name(tree.this.this) == state.preserved_payload:
                payload_creates.append(tree)
        elif body.lstrip().upper().startswith("INSERT INTO"):
            if body.lstrip().upper().startswith("INSERT INTO FUNCTION"):
                continue
            tree = parse_one(body, read=ClickHouseDialect)
            target = tree.this.this if isinstance(tree.this, exp.Schema) else tree.this
            if _name(target) == state.preserved_payload and not any(
                table.alias == "target_dst" for table in tree.expression.find_all(exp.Table)
            ):
                incoming_value_casts.extend(
                    cast.args["to"].sql(dialect=ClickHouseDialect)
                    for cast in tree.expression.find_all(exp.Cast)
                    if isinstance(cast.this, exp.Column) and cast.this.name == "value"
                )
    assert len(payload_creates) == 1
    payload_value = next(
        column
        for column in payload_creates[0].this.expressions
        if isinstance(column, exp.ColumnDef) and column.name == "value"
    )
    assert payload_value.kind.sql(dialect=ClickHouseDialect) == "UInt64"
    assert incoming_value_casts == ["UInt64"]
