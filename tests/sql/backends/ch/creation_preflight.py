from __future__ import annotations

import importlib
from dataclasses import replace
from types import SimpleNamespace

import pytest
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.ch.creation_policy import ClickHouseCreationPolicy
from analytics_toolkit.sql.backends.ch.layout import inspect_existing_layout
from analytics_toolkit.sql.backends.ch.reconfigure_ddl import parse_create_table
from analytics_toolkit.sql.connection.config import get_connection_config
from analytics_toolkit.sql.connection.errors import InvalidSqlInputError, SqlConfigError

from tests.sql._support.ch_transfer_replacement import ReplacementScopeState

module = importlib.import_module("analytics_toolkit.sql.backends.ch.creation_preflight")
TARGET = "analytics.events"
PHYSICAL = "analytics.events_shard"


def policy(**changes):
    return replace(
        ClickHouseCreationPolicy(
            create_distributed_pair=True,
            shard_engine="MergeTree",
            shard_on_cluster="storage",
            distributed_engine_template=(
                "Distributed({cluster}, {database}, {shard_table}, {sharding_key})"
            ),
            distributed_cluster="storage",
            distributed_on_cluster="facades",
            sharding_key="rand()",
            ddl_ready_timeout_seconds=1,
        ),
        **changes,
    )


def options(**changes):
    values = {
        "ch_replace_table": True,
        "table_name": TARGET,
        "connection_key": "ch",
        "query_label": None,
        "ch_only_shard": False,
        "ch_creation_policy": policy(),
    }
    values.update(changes)
    return SimpleNamespace(**values)


@pytest.fixture
def preflight_case(monkeypatch):
    cases = []

    def make(*, pair=True, source_scope="storage", facade_scope="facades", external=False):
        state = ReplacementScopeState()
        state.clusters["facades"] = (state.coordinator,)
        if not pair:
            state.host_tables[state.coordinator].pop(TARGET)
            state._create(
                state.coordinator,
                parse_create_table(
                    f"CREATE TABLE {TARGET} (id UInt64) ENGINE=MergeTree ORDER BY id", TARGET
                ),
                [(1,), (2,), (3,)],
            )
            # Delegation requires an absent companion at the actual destination.
            state.host_tables[state.remote].pop(PHYSICAL)
        if source_scope == "old":
            state.clusters["old"] = (state.remote,)
            state.clusters["storage"] = (state.coordinator,)
            state.host_tables[state.coordinator].pop(PHYSICAL)
        if facade_scope is None:
            state.clusters["facades"] = (state.coordinator, state.remote)
        elif facade_scope == "old":
            state.clusters["old"] = (state.coordinator,)
            state.clusters["facades"] = (state.remote,)
        physical = "analytics.external_storage" if external else PHYSICAL
        if external:
            original = state.host_tables[state.remote].pop(PHYSICAL)
            state._create(
                state.remote,
                parse_create_table(
                    f"CREATE TABLE {physical} (id UInt64) ENGINE=MergeTree ORDER BY id", physical
                ),
                original.rows,
            )
        if pair:
            state.host_tables[state.coordinator].pop(TARGET)
            relation = physical.split(".", 1)[1]
            state._create(
                state.coordinator,
                parse_create_table(
                    f"CREATE TABLE {TARGET} (id UInt64) "
                    f"ENGINE=Distributed('{source_scope}', 'analytics', '{relation}', rand())",
                    TARGET,
                ),
            )
        state.created.clear()
        config = replace(
            get_connection_config("ch"),
            host="coordinator",
            database="analytics",
            driver="http",
            cluster_routing=None,
        )
        connection = state.open(config)
        actual = inspect_existing_layout(connection, TARGET)
        # The source DDL and storage identity come from actual metadata IO.
        # This unit boundary also carries the known facade management scope.
        existing = replace(actual, distributed_on_cluster=facade_scope)
        calls = []
        monkeypatch.setattr(module, "inspect_existing_layout", lambda *_a, **_k: existing)
        monkeypatch.setattr(
            module, "prepare_replacement_layout", lambda *_a, **_k: calls.append("convert")
        )
        case = SimpleNamespace(
            adapter=get_backend_adapter("ch"),
            connection=connection,
            state=state,
            existing=existing,
            calls=calls,
            before=state.snapshot(),
        )
        cases.append(case)
        return case

    yield make
    for case in cases:
        case.connection.close()
        assert case.state.commands == []
        assert case.state.snapshot() == case.before
        assert all(connection.closed for connection in case.state.opened)


def test_replace_preflight_skips_ordinary_create_and_requires_resolved_policy():
    module.preflight_create_table_replacement(object(), object(), options(ch_replace_table=False))
    with pytest.raises(SqlConfigError, match="resolved creation policy"):
        module.preflight_create_table_replacement(
            object(), object(), options(ch_creation_policy=None)
        )


def test_external_storage_replacement_is_rejected_before_conversion(preflight_case):
    case = preflight_case(external=True)
    assert case.existing.owns_physical is False
    with pytest.raises(InvalidSqlInputError, match="externally owned"):
        module.preflight_create_table_replacement(case.adapter, case.connection, options())
    assert case.calls == []


def test_same_storage_requires_explicit_migration_even_when_new_scope_is_empty(preflight_case):
    case = preflight_case(source_scope="old")
    assert PHYSICAL not in case.state.host_tables[case.state.coordinator]
    with pytest.raises(InvalidSqlInputError, match="Physical placement differs"):
        module.preflight_create_table_replacement(case.adapter, case.connection, options())
    assert case.calls == []


@pytest.mark.parametrize("occupied", [False, True])
def test_moving_existing_facade_rejects_collision_or_requires_reconfiguration(
    preflight_case, occupied
):
    case = preflight_case(facade_scope="old")
    if occupied:
        case.state._create(
            case.state.remote,
            parse_create_table(
                f"CREATE TABLE {TARGET} (id UInt64) ENGINE=MergeTree ORDER BY id", TARGET
            ),
            [(value,) for value in range(100)],
        )
        case.before = case.state.snapshot()
    expected = "unowned target" if occupied else "Facade placement differs"
    with pytest.raises(InvalidSqlInputError, match=expected):
        module.preflight_create_table_replacement(case.adapter, case.connection, options())
    assert case.calls == []


def test_local_facade_can_move_to_empty_verified_scope(preflight_case):
    case = preflight_case(facade_scope=None)
    assert TARGET not in case.state.host_tables[case.state.remote]
    module.preflight_create_table_replacement(case.adapter, case.connection, options())
    assert case.calls == []


def test_existing_pair_in_same_verified_scopes_requires_no_migration_or_collision_probe(
    monkeypatch, preflight_case
):
    case = preflight_case()
    monkeypatch.setattr(
        module, "table_exists_on_cluster", lambda *_a: pytest.fail("unnecessary collision probe")
    )
    module.preflight_create_table_replacement(case.adapter, case.connection, options())
    assert case.calls == []


@pytest.mark.parametrize("existing_pair", [False, True])
def test_verified_layout_conversion_is_delegated_only_after_collision_checks(
    preflight_case, existing_pair
):
    case = preflight_case(pair=existing_pair)
    request = options(ch_only_shard=existing_pair)
    module.preflight_create_table_replacement(case.adapter, case.connection, request)
    assert case.calls == ["convert"]


@pytest.mark.parametrize("occupied", [False, True])
def test_new_local_companion_scope_checks_named_target_without_cluster(occupied):
    looked_up = []
    adapter = SimpleNamespace(
        table_exists=lambda _c, table, **_k: looked_up.append(table) or occupied
    )
    request = options(table_name="db.events", ch_creation_policy=policy(shard_on_cluster=None))
    if occupied:
        with pytest.raises(InvalidSqlInputError, match="Destination companion"):
            module._reject_companion_collision(adapter, object(), request)
    else:
        module._reject_companion_collision(adapter, object(), request)
    assert looked_up == ["db.events_shard"]


def test_scope_aliases_use_verified_endpoints_and_local_scopes_do_not_equal_remote(preflight_case):
    case = preflight_case()
    case.state.clusters["alias"] = (case.state.remote,)
    case.state.clusters["physical"] = (case.state.remote,)
    assert module._same_storage_scope(case.connection, "alias", "physical") is True
    assert module._same_storage_scope(case.connection, None, "physical") is False
    assert module._same_storage_scope(case.connection, None, None) is True
    request = options(ch_creation_policy=policy(distributed_on_cluster=None))
    module._reject_facade_collision(case.connection, request)
