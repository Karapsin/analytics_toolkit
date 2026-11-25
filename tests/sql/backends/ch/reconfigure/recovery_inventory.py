"""Public structural failures retain exact recovery resources and primary errors."""

from __future__ import annotations

import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends.ch.cutover import StructuralCutoverError
from analytics_toolkit.sql.connection.errors import AmbiguousSqlReplaceError

from tests.sql._support.ch_recovery_inventory import (
    assert_original_resources_retained,
    command_parts,
    created_scopes,
    install_recovery,
    original_snapshots_unchanged,
)


def _reconfigure(**options):
    return sql.ch_reconfigure_table(
        "ch",
        "analytics.events",
        order_by="id DESC",
        ch_distributed_table=True,
        ch_shard_on_cluster="storage",
        ch_distributed_cluster="storage",
        retry_cnt=2,
        timeout_increment=0,
        **options,
    )


def _scopes(error):
    return tuple(getattr(error, "analytics_toolkit_sql_recovery_table_scopes", ()))


def _has_cause(error, expected):
    pending, visited = [error], set()
    while pending:
        current = pending.pop()
        if current is expected:
            return True
        if id(current) in visited:
            continue
        visited.add(id(current))
        pending.extend(
            cause for cause in (current.__cause__, current.__context__) if cause is not None
        )
    return False


def _closed(state):
    assert state.opened
    assert all(connection.close_calls == 1 for connection in state.opened)


@pytest.mark.parametrize("driver", ["http", "native"])
def test_public_reconfigure_reports_applied_create_with_lost_acknowledgement(
    monkeypatch, write_sql_connections, driver
):
    state = install_recovery(monkeypatch, write_sql_connections, driver=driver)
    before = state.snapshot()
    failure = ConnectionError("replacement CREATE acknowledgement lost")
    applied = []

    def fail_after_create(_connection, statement):
        kind, scope, _body = command_parts(statement)
        if kind == "CREATE" and not applied:
            applied.append((statement, scope))
            raise failure

    state.after_raw_command = fail_after_create

    with pytest.raises(
        (ConnectionError, StructuralCutoverError, AmbiguousSqlReplaceError)
    ) as caught:
        _reconfigure()

    original_snapshots_unchanged(state, before)
    assert len(applied) == 1
    assert len(state.created) == 1
    assert created_scopes(state) == ((state.created[0][1], "storage"),)
    assert _has_cause(caught.value, failure)
    assert set(created_scopes(state)) <= set(_scopes(caught.value))
    assert not any(
        command_parts(statement)[0] in {"DROP", "EXCHANGE", "RENAME"}
        for statement in state.commands
    )
    assert (
        len([statement for statement in state.commands if command_parts(statement)[0] == "CREATE"])
        == 1
    )
    _closed(state)


@pytest.mark.parametrize("driver", ["http", "native"])
def test_public_reconfigure_interrupt_after_applied_cutover_preserves_inventory(
    monkeypatch, write_sql_connections, driver
):
    state = install_recovery(monkeypatch, write_sql_connections, driver=driver)
    before = state.snapshot()
    interruption = KeyboardInterrupt("cutover acknowledgement interrupted")
    applied = []

    def interrupt_after_cutover(_connection, statement):
        kind, scope, _body = command_parts(statement)
        if kind == "EXCHANGE" and not applied:
            applied.append((statement, scope))
            raise interruption

    state.after_raw_command = interrupt_after_cutover

    with pytest.raises(KeyboardInterrupt) as caught:
        _reconfigure()

    assert caught.value is interruption
    assert_original_resources_retained(state, before)
    assert len(applied) == 1
    assert len(created_scopes(state)) == 2
    assert set(created_scopes(state)) <= set(_scopes(caught.value))
    journal = getattr(caught.value, "analytics_toolkit_sql_cutover_journal", ())
    assert len(journal) == 1
    assert journal[0]["submitted"] is True
    assert journal[0]["acknowledged"] is False
    assert not any(command_parts(statement)[0] == "DROP" for statement in state.commands)
    _closed(state)


@pytest.mark.parametrize("driver", ["http", "native"])
def test_public_reconfigure_cleanup_failure_reports_scope_and_preserves_count_failure(
    monkeypatch, write_sql_connections, driver
):
    state = install_recovery(monkeypatch, write_sql_connections, driver=driver)
    before = state.snapshot()
    cleanup_failure = ConnectionError("cleanup DROP acknowledgement lost")
    corrupted, dropped = [], []

    def fail_cleanup_after_partial_copy(_connection, statement):
        kind, scope, _body = command_parts(statement)
        if kind == "INSERT" and not corrupted:
            physical = next(name for host, name in state.created if host == state.remote)
            state.host_tables[state.remote][physical].rows.pop()
            corrupted.append(physical)
        elif kind == "DROP" and not dropped:
            dropped.append((statement, scope))
            raise cleanup_failure

    state.after_raw_command = fail_cleanup_after_partial_copy

    with pytest.raises(RuntimeError, match="count") as caught:
        _reconfigure()

    original_snapshots_unchanged(state, before)
    assert len(corrupted) == len(dropped) == 1
    assert not any(
        command_parts(statement)[0] in {"EXCHANGE", "RENAME"} for statement in state.commands
    )
    assert set(created_scopes(state)) <= set(_scopes(caught.value))
    cleanup_errors = getattr(caught.value, "analytics_toolkit_sql_cleanup_errors", ())
    assert any(_has_cause(error, cleanup_failure) for error in cleanup_errors)
    _closed(state)


@pytest.mark.parametrize("driver", ["http", "native"])
def test_public_reconfigure_interrupt_after_applied_rollback_preserves_inventory(
    monkeypatch, write_sql_connections, driver
):
    state = install_recovery(monkeypatch, write_sql_connections, driver=driver)
    before = state.snapshot()
    interruption = KeyboardInterrupt("rollback acknowledgement interrupted")
    exchanges = []

    def interrupt_after_rollback(_connection, statement):
        kind, scope, _body = command_parts(statement)
        if kind != "EXCHANGE":
            return
        exchanges.append((statement, scope))
        if len(exchanges) == 1:
            state.host_tables[state.remote]["analytics.events_shard"].rows.append((999,))
        elif len(exchanges) == 2:
            raise interruption

    state.after_raw_command = interrupt_after_rollback

    with pytest.raises(KeyboardInterrupt) as caught:
        _reconfigure()

    assert caught.value is interruption
    assert len(exchanges) == 2
    original_snapshots_unchanged(state, before)
    assert set(created_scopes(state)) <= set(_scopes(caught.value))
    journal = getattr(caught.value, "analytics_toolkit_sql_cutover_journal", ())
    assert len(journal) == 1
    assert journal[0]["acknowledged"] is True
    assert journal[0]["rollback_submitted"] is True
    assert journal[0].get("rollback_acknowledged") is not True
    assert not any(command_parts(statement)[0] == "DROP" for statement in state.commands)
    _closed(state)


@pytest.mark.parametrize("driver", ["http", "native"])
def test_public_replacement_rollback_failure_retains_both_scopes_and_primary_failure(
    monkeypatch, write_sql_connections, driver
):
    state = install_recovery(monkeypatch, write_sql_connections, driver=driver)
    before = state.snapshot()
    rollback_failure = ConnectionError("rollback execution unavailable")
    exchanges = []
    failed_rollbacks = []

    def make_final_count_differ(_connection, statement):
        kind, scope, _body = command_parts(statement)
        if kind == "EXCHANGE":
            exchanges.append((statement, scope))
            if len(exchanges) == 2:
                state.host_tables[state.remote]["analytics.events_shard"].rows.append((999,))

    def reject_rollback(_connection, statement):
        kind, scope, _body = command_parts(statement)
        if kind == "EXCHANGE" and len(exchanges) == 2:
            failed_rollbacks.append((statement, scope))
            raise rollback_failure

    state.after_raw_command = make_final_count_differ
    state.before_raw_command = reject_rollback
    incoming = pd.DataFrame({"id": pd.Series([201, 202], dtype="uint64")})
    original = incoming.copy(deep=True)

    with pytest.raises(AmbiguousSqlReplaceError, match="automatic rollback") as caught:
        sql.load_df(
            "ch",
            "analytics.events",
            incoming,
            table_schema={"id": "UInt64"},
            write_mode="replace",
            retry_cnt=2,
            timeout_increment=0,
        )

    assert len(exchanges) == 2
    assert len(failed_rollbacks) == 1
    assert_original_resources_retained(state, before)
    assert "final target row count" in str(caught.value.__cause__)
    assert _has_cause(caught.value.rollback_error, rollback_failure)
    recovery = _scopes(caught.value)
    assert set(created_scopes(state)) <= set(recovery)
    assert {scope for _name, scope in recovery} >= {None, "storage"}
    journal = getattr(caught.value, "analytics_toolkit_sql_cutover_journal", ())
    assert len(journal) == 2
    assert all(step["acknowledged"] is True for step in journal)
    assert journal[-1]["rollback_submitted"] is True
    assert journal[-1].get("rollback_acknowledged") is not True
    assert "rollback_submitted" not in journal[0]
    assert not any(command_parts(statement)[0] == "DROP" for statement in state.commands)
    pd.testing.assert_frame_equal(incoming, original)
    _closed(state)
