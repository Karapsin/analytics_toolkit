"""Physical failures preserve original host datasets and primary outcomes."""

from __future__ import annotations

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from analytics_toolkit.sql.connection.errors import InvalidSqlInputError
from sqlglot import exp, parse_one

from tests.sql._support.reconfigure_physical_faults import (
    assert_no_live_cutover,
    assert_original_physical_state,
    before_physical_execution,
    install_physical_faults,
    install_unrelated_connection,
    reselect_private_config,
)
from tests.sql._support.reconfigure_physical_scope import reconfigure_physical


@pytest.mark.parametrize(
    "identity",
    [
        [],
        [("00000000-0000-0000-0000-000000000099", 9000)],
        [("00000000-0000-0000-0000-000000000002", 9001)],
    ],
    ids=["missing", "different-server", "different-native-port"],
)
def test_physical_worker_identity_must_match_before_any_replacement_ddl(
    monkeypatch, write_sql_connections, identity
):
    raw = install_physical_faults(monkeypatch, write_sql_connections)
    raw.worker_identity = identity
    original = raw.snapshot()

    with pytest.raises(InvalidSqlInputError, match="physical worker endpoint identity"):
        reconfigure_physical()

    assert raw.commands == raw.worker_commands == []
    assert raw.counts == []
    assert raw.factory_attempts == [("ch", raw.remote, 9000)]
    assert len(raw.worker_connections) == 1
    assert raw.worker_connections[0].close_attempts == 1
    assert_original_physical_state(raw, original)


@pytest.mark.parametrize("rows", [[], [(100, 100)]], ids=["missing", "extra-fields"])
def test_physical_endpoint_count_shape_rejects_before_any_replacement_ddl(
    monkeypatch, write_sql_connections, rows
):
    raw = install_physical_faults(monkeypatch, write_sql_connections)
    raw.worker_count = rows
    original = raw.snapshot()

    with pytest.raises(InvalidSqlInputError, match="Incomplete physical endpoint row-count"):
        reconfigure_physical()

    assert raw.commands == raw.worker_commands == []
    assert len(raw.worker_connections) == 1
    assert_original_physical_state(raw, original)


@pytest.mark.parametrize("rows", [[], [(3, 3)]], ids=["missing", "extra-fields"])
def test_physical_aggregate_count_shape_rejects_before_any_replacement_ddl(
    monkeypatch, write_sql_connections, rows
):
    raw = install_physical_faults(monkeypatch, write_sql_connections, replicas=1)
    raw.aggregate_count = rows
    original = raw.snapshot()

    with pytest.raises(InvalidSqlInputError, match="Incomplete physical row-count"):
        reconfigure_physical()

    assert raw.commands == raw.worker_commands == []
    assert raw.worker_connections == []
    assert_original_physical_state(raw, original)


def test_physical_configuration_change_after_plan_rejects_before_worker_open(
    monkeypatch, write_sql_connections, capsys
):
    raw = install_physical_faults(monkeypatch, write_sql_connections)
    original = raw.snapshot()
    before_physical_execution(monkeypatch, lambda: reselect_private_config(write_sql_connections))

    with pytest.raises(InvalidSqlInputError, match="configuration changed after") as error:
        reconfigure_physical()

    assert raw.factory_attempts == raw.worker_connections == raw.commands == []
    assert_original_physical_state(raw, original)
    assert "private-config-change-must-stay-hidden" not in str(error.value)
    assert "private-config-change-must-stay-hidden" not in capsys.readouterr().out


def test_physical_configuration_guard_checks_factory_selected_config_before_open(
    monkeypatch, write_sql_connections, capsys
):
    raw = install_physical_faults(monkeypatch, write_sql_connections)
    original = raw.snapshot()
    raw.factory_callback = lambda: reselect_private_config(write_sql_connections)

    with pytest.raises(InvalidSqlInputError, match="configuration changed during") as error:
        reconfigure_physical()

    assert raw.factory_attempts == [("ch", raw.remote, 9000)]
    assert raw.worker_connections == raw.commands == raw.counts == []
    assert_original_physical_state(raw, original)
    assert "private-config-change-must-stay-hidden" not in str(error.value)
    assert "private-config-change-must-stay-hidden" not in capsys.readouterr().out


def test_physical_configuration_guard_allows_an_independent_nested_connection(
    monkeypatch, write_sql_connections
):
    raw = install_physical_faults(monkeypatch, write_sql_connections)
    original = raw.physical_counters()
    opened = install_unrelated_connection(monkeypatch, write_sql_connections)

    def connect_other():
        connection = sql.get_sql_connection("other")
        connection.close()

    raw.factory_callback = connect_other

    result = reconfigure_physical()

    assert result.metadata.final_target_rows == 103
    assert raw.physical_counters() == original
    assert len(opened) == 1
    assert opened[0].closed
    assert len(raw.worker_connections) == 1
    assert raw.worker_connections[0].closed
    assert raw.closed


def test_physical_configuration_drift_stops_next_host_and_cleans_only_owned_stages(
    monkeypatch, write_sql_connections
):
    raw = install_physical_faults(monkeypatch, write_sql_connections)
    original = raw.snapshot()

    def change_after_first_copy(statement):
        parsed = parse_one(statement, read=ClickHouseDialect)
        if isinstance(parsed, exp.Insert) and parsed.this.name.startswith(
            "events_shard__reconfigure_"
        ):
            reselect_private_config(write_sql_connections)

    raw.command_callback = change_after_first_copy

    with pytest.raises(InvalidSqlInputError, match="configuration changed during"):
        reconfigure_physical()

    assert raw.worker_commands == []
    assert len([sql for sql in raw.commands if sql.startswith("INSERT INTO")]) == 1
    assert_no_live_cutover(raw)
    assert_original_physical_state(raw, original)


def test_physical_replacement_count_drift_cleans_owned_stages_before_cutover(
    monkeypatch, write_sql_connections
):
    raw = install_physical_faults(monkeypatch, write_sql_connections)
    raw.extra_replacement_row = True
    original = raw.snapshot()

    with pytest.raises(RuntimeError, match="Physical per-host row counts changed"):
        reconfigure_physical()

    assert raw.worker_commands
    assert_no_live_cutover(raw)
    assert_original_physical_state(raw, original)


def test_physical_final_count_drift_rolls_back_acknowledged_cutover_and_closes_workers(
    monkeypatch, write_sql_connections
):
    raw = install_physical_faults(monkeypatch, write_sql_connections)
    raw.corrupt_final = True
    original = raw.snapshot()

    with pytest.raises(RuntimeError, match="Physical per-host row counts changed"):
        reconfigure_physical()

    assert len([sql for sql in raw.commands if sql.startswith("EXCHANGE TABLES")]) == 2
    assert_original_physical_state(raw, original)


def test_physical_worker_close_failure_keeps_success_without_replaying_any_copy(
    monkeypatch, write_sql_connections, capsys
):
    raw = install_physical_faults(monkeypatch, write_sql_connections)
    raw.fail_worker_close = True
    original = raw.physical_counters()

    result = sql.ch_reconfigure_table(
        "ch",
        "analytics.events",
        order_by="cityHash64(id)",
        retry_cnt=3,
        timeout_increment=0,
        return_metadata=True,
    )

    assert result.metadata.source_rows == result.metadata.staged_rows == 103
    assert result.metadata.final_target_rows == 103
    assert result.metadata.row_count_validated is True
    assert result.data["cleanup_complete"] is True
    assert raw.physical_counters() == original
    assert len(raw.wrappers) == len(raw.worker_commands) == 1
    assert raw.worker_connections[0].close_attempts == 1
    assert raw.worker_connections[0].closed
    assert raw.closed
    assert "close_host" in capsys.readouterr().out


def test_physical_worker_close_failure_preserves_primary_count_error_and_originals(
    monkeypatch, write_sql_connections, capsys
):
    raw = install_physical_faults(monkeypatch, write_sql_connections)
    raw.fail_worker_close = True
    raw.worker_count = []
    original = raw.snapshot()

    with pytest.raises(InvalidSqlInputError, match="Incomplete physical endpoint row-count"):
        reconfigure_physical()

    assert raw.worker_connections[0].close_attempts == 1
    assert raw.commands == []
    assert_original_physical_state(raw, original)
    assert "close_host" in capsys.readouterr().out
