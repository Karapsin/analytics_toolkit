"""Replacement scope decisions require complete, actual endpoint metadata."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace

import pytest
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.ch.creation_policy import (
    resolve_clickhouse_creation_policy,
)
from analytics_toolkit.sql.backends.ch.layout import inspect_existing_layout
from analytics_toolkit.sql.backends.ch.reconfigure import (
    execute_ch_table_reconfiguration,
    plan_ch_table_reconfiguration,
)
from analytics_toolkit.sql.backends.ch.reconfigure_ddl import parse_create_table
from analytics_toolkit.sql.backends.ch.reconfigure_models import ChReconfigureOptions
from analytics_toolkit.sql.backends.ch.reconfigure_physical import PhysicalRebuildExecutionScope
from analytics_toolkit.sql.backends.ch.replacement_scope import (
    certify_replacement_scope,
    inspect_completed_physical_layout,
)
from analytics_toolkit.sql.connection.config import get_connection_config
from analytics_toolkit.sql.connection.errors import InvalidSqlInputError

from tests.sql._support.ch_transfer_replacement import ReplacementScopeState
from tests.sql._support.reconfigure_conversion_scope import configure_conversion
from tests.sql._support.reconfigure_physical_faults import reselect_private_config

_TARGET = "analytics.events"
_LOCAL = "analytics.local_events"


@pytest.fixture
def replacement_context(monkeypatch, write_sql_connections):
    configure_conversion(write_sql_connections)
    config = get_connection_config("ch")
    state = ReplacementScopeState()
    monkeypatch.setattr(get_backend_adapter("ch"), "open_connection", state.open)
    connection = state.open(config)
    try:
        yield state, connection, config.ddl_defaults.regular
    finally:
        connection.close()


def _policy(scope, *, physical, pair=False, facade=None, routing="storage_alias"):
    if physical is None:
        scope = replace(scope, shard=replace(scope.shard, on_cluster=None))
    return resolve_clickhouse_creation_policy(
        scope,
        ch_engine="MergeTree",
        ch_cluster=None,
        ch_sharding_key="rand()",
        ch_distributed_table=pair,
        ch_only_shard=False,
        ch_distributed_engine_template=None,
        ch_distributed_cluster=routing,
        ch_shard_on_cluster=physical,
        ch_distributed_on_cluster=facade,
    )


def _local_table(state):
    definition = parse_create_table(
        "CREATE TABLE analytics.local_events (id UInt64) ENGINE=MergeTree ORDER BY id",
        _LOCAL,
    )
    state._create(state.coordinator, definition, [(7,), (8,)])


@contextmanager
def _known_facade_scope(monkeypatch, write_sql_connections, original, desired):
    configure_conversion(write_sql_connections)
    config = get_connection_config("ch")
    state = ReplacementScopeState()
    state.clusters["core_alias"] = state.clusters["core"]
    if original == "core":
        state._create(state.remote, state.host_tables[state.coordinator][_TARGET].create.copy())
    monkeypatch.setattr(get_backend_adapter("ch"), "open_connection", state.open)
    connection = state.open(config)
    try:
        layout = inspect_existing_layout(
            connection, _TARGET, require_owned=True, distributed_on_cluster=original
        )
        policy = _policy(
            config.ddl_defaults.regular, physical="storage_alias", pair=True, facade=desired
        )
        assert layout.distributed_on_cluster == original
        assert policy.distributed_on_cluster == desired
        yield state, connection, layout, policy
    finally:
        connection.close()
        assert state.opened
        assert all(opened.closed for opened in state.opened)


def _completed_conversion(context, *, physical):
    state, connection, defaults = context
    foreign = state.snapshot()[state.coordinator]["analytics.events_shard"]
    desired = replace(
        defaults,
        create_distributed_pair=False,
        shard=replace(defaults.shard, on_cluster=physical),
        distributed=replace(defaults.distributed, cluster="storage", on_cluster="coordinator_only"),
    )
    options = ChReconfigureOptions(
        connection_key="ch",
        table=_TARGET,
        ch_distributed_table=False,
        ch_distributed_on_cluster="coordinator_only",
        to_defaults=True,
        regular_defaults=desired,
    )
    adapter = get_backend_adapter("ch")
    change = plan_ch_table_reconfiguration(adapter, connection, options)
    execute_ch_table_reconfiguration(adapter, connection, change, validate_row_count=True)
    assert change.strategy == "pair_to_local"
    assert change.cleanup_complete is True
    assert change.source_count == change.replacement_count == change.final_count == 3
    assert state.snapshot()[state.coordinator]["analytics.events_shard"] == foreign
    destination = state.remote if physical is not None else state.coordinator
    assert state.rows(destination, _TARGET) == [(11,), (12,), (13,)]
    assert "analytics.events_shard" not in state.host_tables[state.remote]
    execution = change.physical_execution
    assert isinstance(execution, PhysicalRebuildExecutionScope)
    return execution


def _assert_completed_state(state, snapshot, commands, *, physical):
    assert state.snapshot() == snapshot
    assert state.commands == commands
    destination = state.remote if physical is not None else state.coordinator
    assert state.rows(destination, _TARGET) == [(11,), (12,), (13,)]
    assert state.rows(state.coordinator, "analytics.events_shard") == state.foreign_rows


@pytest.mark.parametrize(
    ("original", "desired"),
    [("coordinator_only", None), (None, "coordinator_only")],
    ids=["named-current-to-local", "local-to-named-current"],
)
def test_replacement_accepts_named_and_local_scopes_for_the_same_verified_coordinator(
    replacement_context, original, desired
):
    state, connection, defaults = replacement_context
    _local_table(state)
    layout = inspect_existing_layout(
        connection, _LOCAL, require_owned=True, shard_on_cluster=original
    )
    policy = _policy(defaults, physical=desired)
    before = state.snapshot()
    assert policy.shard_on_cluster == desired

    certify_replacement_scope(connection, layout, policy, staged_exchange=True)

    assert layout.owns_physical is True
    assert layout.physical_table == _LOCAL
    assert state.snapshot() == before
    assert state.rows(state.coordinator, _LOCAL) == [(7,), (8,)]
    assert state.commands == []


@pytest.mark.parametrize("source_cluster", ["storage", "core"], ids=["remote", "two-hosts"])
def test_replacement_cannot_collapse_remote_or_multi_host_storage_to_the_local_coordinator(
    replacement_context, source_cluster
):
    state, connection, defaults = replacement_context
    if source_cluster == "core":
        definition = parse_create_table(
            "CREATE TABLE analytics.events (id UInt64) "
            "ENGINE=Distributed('core', 'analytics', 'events_shard', rand())",
            _TARGET,
        )
        state.host_tables[state.coordinator].pop(_TARGET)
        state._create(state.coordinator, definition)
    layout = inspect_existing_layout(connection, _TARGET, require_owned=True)
    policy = _policy(defaults, physical=None, pair=True)
    before = state.snapshot()
    assert policy.shard_on_cluster is None

    with pytest.raises(InvalidSqlInputError, match="Physical placement differs"):
        certify_replacement_scope(connection, layout, policy, staged_exchange=True)

    assert layout.distributed_cluster == source_cluster
    assert state.snapshot() == before
    assert state.rows(state.remote, "analytics.events_shard") == [(11,), (12,), (13,)]
    assert state.rows(state.coordinator, "analytics.events_shard") == state.foreign_rows
    assert state.commands == []


def test_direct_facade_recreation_allows_only_verified_existing_and_empty_extra_hosts(
    replacement_context,
):
    state, connection, defaults = replacement_context
    layout = inspect_existing_layout(connection, _TARGET, require_owned=True)
    policy = _policy(defaults, physical="storage_alias", pair=True, facade="core")
    before = state.snapshot()
    assert _TARGET not in state.host_tables[state.remote]

    certify_replacement_scope(connection, layout, policy, staged_exchange=False)

    assert state.snapshot() == before
    assert state.rows(state.coordinator, _TARGET) == [(11,), (12,), (13,)]
    assert _TARGET not in state.host_tables[state.remote]
    assert state.commands == []


def test_direct_facade_recreation_rejects_an_empty_scope_excluding_the_existing_coordinator(
    replacement_context,
):
    state, connection, defaults = replacement_context
    layout = inspect_existing_layout(connection, _TARGET, require_owned=True)
    policy = _policy(defaults, physical="storage_alias", pair=True, facade="frontends")
    before = state.snapshot()
    assert _TARGET not in state.host_tables[state.remote]

    with pytest.raises(InvalidSqlInputError, match="existing facade ownership"):
        certify_replacement_scope(connection, layout, policy, staged_exchange=False)

    assert state.snapshot() == before
    assert state.rows(state.coordinator, _TARGET) == [(11,), (12,), (13,)]
    assert _TARGET not in state.host_tables[state.remote]
    assert state.commands == []


def test_staged_facade_exchange_rejects_an_empty_additional_host(replacement_context):
    state, connection, defaults = replacement_context
    layout = inspect_existing_layout(connection, _TARGET, require_owned=True)
    policy = _policy(defaults, physical="storage_alias", pair=True, facade="core")
    before = state.snapshot()
    assert _TARGET not in state.host_tables[state.remote]

    with pytest.raises(InvalidSqlInputError, match="every original facade"):
        certify_replacement_scope(connection, layout, policy, staged_exchange=True)

    assert state.snapshot() == before
    assert state.rows(state.coordinator, _TARGET) == [(11,), (12,), (13,)]
    assert _TARGET not in state.host_tables[state.remote]
    assert state.commands == []


@pytest.mark.parametrize("desired", [None, "coordinator_only"], ids=["local", "named-subset"])
def test_staged_exchange_rejects_narrowing_a_known_complete_facade_scope(
    monkeypatch, write_sql_connections, desired
):
    with _known_facade_scope(monkeypatch, write_sql_connections, "core", desired) as context:
        state, connection, layout, policy = context
        before = state.snapshot()

        with pytest.raises(InvalidSqlInputError, match=r"[Ff]acade.*[Pp]lacement|[Ff]acade.*scope"):
            certify_replacement_scope(connection, layout, policy, staged_exchange=True)

        assert state.snapshot() == before
        assert state.rows(state.coordinator, _TARGET) == [(11,), (12,), (13,)]
        assert state.rows(state.remote, _TARGET) == [(11,), (12,), (13,)]
        assert state.rows(state.coordinator, "analytics.events_shard") == state.foreign_rows
        assert state.rows(state.remote, "analytics.events_shard") == [(11,), (12,), (13,)]
        assert state.commands == []


@pytest.mark.parametrize(
    ("original", "desired"),
    [("core", "core_alias"), ("coordinator_only", None), (None, "coordinator_only")],
    ids=["complete-scope-alias", "named-singleton-to-local", "local-to-named-singleton"],
)
def test_staged_exchange_accepts_equivalent_verified_facade_execution_scopes(
    monkeypatch, write_sql_connections, original, desired
):
    with _known_facade_scope(monkeypatch, write_sql_connections, original, desired) as context:
        state, connection, layout, policy = context
        before = state.snapshot()

        certify_replacement_scope(connection, layout, policy, staged_exchange=True)

        assert state.snapshot() == before
        assert state.rows(state.coordinator, _TARGET) == [(11,), (12,), (13,)]
        assert state.rows(state.coordinator, "analytics.events_shard") == state.foreign_rows
        assert state.rows(state.remote, "analytics.events_shard") == [(11,), (12,), (13,)]
        assert state.commands == []


def test_replacement_rejects_physical_ddl_changed_since_verified_layout_inspection(
    replacement_context,
):
    state, connection, defaults = replacement_context
    layout = inspect_existing_layout(connection, _TARGET, require_owned=True)
    physical = "analytics.events_shard"
    state.host_tables[state.remote][physical].create = parse_create_table(
        "CREATE TABLE analytics.events_shard (id UInt64) ENGINE=MergeTree ORDER BY tuple()",
        physical,
    )
    policy = _policy(defaults, physical="storage_alias", pair=True)
    before = state.snapshot()

    with pytest.raises(InvalidSqlInputError, match="Existing physical table definitions differ"):
        certify_replacement_scope(connection, layout, policy, staged_exchange=True)

    assert state.snapshot() == before
    assert state.rows(state.remote, physical) == [(11,), (12,), (13,)]
    assert state.rows(state.coordinator, _TARGET) == [(11,), (12,), (13,)]
    assert state.commands == []


def test_replacement_routing_cannot_reach_unowned_storage_with_matching_table_names(
    replacement_context,
):
    state, connection, defaults = replacement_context
    layout = inspect_existing_layout(connection, _TARGET, require_owned=True)
    policy = _policy(defaults, physical="storage_alias", pair=True, routing="incoming")
    before = state.snapshot()

    with pytest.raises(InvalidSqlInputError, match="Distributed replacement routing differs"):
        certify_replacement_scope(connection, layout, policy, staged_exchange=True)

    assert state.snapshot() == before
    assert state.rows(state.remote, "analytics.events_shard") == [(11,), (12,), (13,)]
    assert state.rows(state.coordinator, "analytics.events_shard") == state.foreign_rows
    assert state.rows(state.coordinator, _TARGET) == [(11,), (12,), (13,)]
    assert state.commands == []


def test_completed_conversion_rejects_configuration_identity_drift(
    replacement_context, write_sql_connections
):
    state, connection, _defaults = replacement_context
    execution = _completed_conversion(replacement_context, physical="storage")
    snapshot, commands = state.snapshot(), list(state.commands)
    reselect_private_config(write_sql_connections)

    with pytest.raises(InvalidSqlInputError, match="configuration changed after") as caught:
        inspect_completed_physical_layout(connection, execution)

    assert "private-config-change-must-stay-hidden" not in str(caught.value)
    _assert_completed_state(state, snapshot, commands, physical="storage")


@pytest.mark.parametrize("identity", ["native-endpoint", "server-instance"])
def test_completed_conversion_rejects_changed_remote_execution_scope(replacement_context, identity):
    state, connection, _defaults = replacement_context
    execution = _completed_conversion(replacement_context, physical="storage")
    snapshot, commands = state.snapshot(), list(state.commands)
    if identity == "native-endpoint":
        name, address, port = state.host_endpoints[state.remote]
        state.host_endpoints[state.remote] = (name, address, port + 1)
    else:
        _identity, port = state.server_identities[state.remote]
        state.server_identities[state.remote] = ("00000000-0000-0000-0000-000000000099", port)

    with pytest.raises(InvalidSqlInputError, match="completed physical conversion endpoint scope"):
        inspect_completed_physical_layout(connection, execution)

    _assert_completed_state(state, snapshot, commands, physical="storage")


def test_completed_conversion_rejects_changed_coordinator_local_target_definition(
    replacement_context,
):
    state, connection, _defaults = replacement_context
    execution = _completed_conversion(replacement_context, physical=None)
    state.host_tables[state.coordinator][_TARGET].create = parse_create_table(
        "CREATE TABLE analytics.events (id UInt64) ENGINE=MergeTree ORDER BY tuple()", _TARGET
    )
    snapshot, commands = state.snapshot(), list(state.commands)

    with pytest.raises(InvalidSqlInputError, match="completed local conversion target definition"):
        inspect_completed_physical_layout(connection, execution)

    _assert_completed_state(state, snapshot, commands, physical=None)


def test_direct_facade_recreation_preserves_a_verified_coordinator_local_facade(
    replacement_context,
):
    state, connection, defaults = replacement_context
    layout = inspect_existing_layout(connection, _TARGET, require_owned=True)
    policy = _policy(defaults, physical="storage_alias", pair=True, facade=None)
    assert policy.distributed_on_cluster is None
    snapshot = state.snapshot()

    certify_replacement_scope(connection, layout, policy, staged_exchange=False)

    assert state.snapshot() == snapshot
    assert state.rows(state.coordinator, _TARGET) == [(11,), (12,), (13,)]
    assert state.commands == []


def test_replacement_cannot_claim_an_actual_externally_referenced_physical_table(
    replacement_context,
):
    state, connection, defaults = replacement_context
    external = "analytics.external_storage"
    definition = parse_create_table(
        "CREATE TABLE analytics.external_storage (id UInt64) ENGINE=MergeTree ORDER BY id",
        external,
    )
    external_rows = [(91,), (92,)]
    state._create(state.remote, definition, external_rows)
    facade = parse_create_table(
        "CREATE TABLE analytics.events (id UInt64) "
        "ENGINE=Distributed('storage', 'analytics', 'external_storage', rand())",
        _TARGET,
    )
    state.host_tables[state.coordinator].pop(_TARGET)
    state._create(state.coordinator, facade)
    layout = inspect_existing_layout(connection, _TARGET, require_owned=False)
    assert layout.owns_physical is False
    policy = _policy(defaults, physical="storage_alias", pair=True)
    snapshot = state.snapshot()

    with pytest.raises(InvalidSqlInputError, match="externally owned physical storage"):
        certify_replacement_scope(connection, layout, policy, staged_exchange=True)

    assert state.snapshot() == snapshot
    assert state.rows(state.remote, external) == external_rows
    assert state.rows(state.coordinator, _TARGET) == external_rows
    assert state.rows(state.remote, "analytics.events_shard") == [(11,), (12,), (13,)]
    assert state.rows(state.coordinator, "analytics.events_shard") == state.foreign_rows
    assert state.commands == []


def test_replacement_rejects_a_lost_original_storage_binding_without_claiming_local_shadow(
    replacement_context,
):
    state, connection, defaults = replacement_context
    verified = inspect_existing_layout(connection, _TARGET, require_owned=True)
    assert verified.distributed_cluster == "storage"
    incomplete = replace(verified, distributed_cluster=None)
    policy = _policy(defaults, physical=None, pair=True)
    snapshot = state.snapshot()

    with pytest.raises(InvalidSqlInputError, match="existing Distributed storage scope"):
        certify_replacement_scope(connection, incomplete, policy, staged_exchange=True)

    assert state.snapshot() == snapshot
    assert state.rows(state.remote, "analytics.events_shard") == [(11,), (12,), (13,)]
    assert state.rows(state.coordinator, "analytics.events_shard") == state.foreign_rows
    assert state.rows(state.coordinator, _TARGET) == [(11,), (12,), (13,)]
    assert state.commands == []
