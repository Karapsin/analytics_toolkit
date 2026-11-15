"""Public drops may remove only verified resources in each table role's scope."""

from __future__ import annotations

from copy import deepcopy

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends.ch.cluster_functions import cluster_arguments
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from analytics_toolkit.sql.backends.ch.reconfigure_ddl import parse_create_table
from analytics_toolkit.sql.connection.errors import InvalidSqlInputError
from sqlglot import exp, parse_one

from tests.sql._support.assertions import strip_query_label
from tests.sql._support.ch_drop_scope import install_drop_scope

_TARGET = "analytics.events"
_PHYSICAL = "analytics.events_shard"


def _drop(table, *, shard=True, facade=True, cluster="storage"):
    return sql.drop_tables(
        "ch",
        table,
        ch_cluster=cluster,
        if_exists=True,
        ch_drop_shard=shard,
        ch_drop_distributed=facade,
        ch_wait_for_absence=True,
        ch_wait_timeout_seconds=1,
        ch_wait_poll_interval_seconds=0.001,
        ch_retry_per_host_drops=True,
    )


def _drop_resources(statements):
    resources = []
    for statement in statements:
        tree = parse_one(strip_query_label(statement), read=ClickHouseDialect)
        assert isinstance(tree, exp.Drop)
        cluster = tree.find(exp.OnCluster)
        scope = cluster.this.name if cluster is not None else None
        resources.extend(
            (f"{table.db}.{table.name}", scope) for table in tree.args.get("tables") or [tree.this]
        )
    return resources


def _filtered_table_names(tree):
    names = set()
    for comparison in tree.find_all(exp.EQ):
        if (
            isinstance(comparison.this, exp.Column)
            and comparison.this.name in {"name", "table"}
            and isinstance(comparison.expression, exp.Literal)
        ):
            names.add(comparison.expression.this)
    for comparison in tree.find_all(exp.In):
        if isinstance(comparison.this, exp.Column) and comparison.this.name in {"name", "table"}:
            names.update(
                item.this for item in comparison.expressions if isinstance(item, exp.Literal)
            )
    return names


def _absence_resources(state, host, statement):
    body = strip_query_label(statement)
    if body.startswith("EXISTS TABLE "):
        target = (
            parse_one(f"SELECT * FROM {body[len('EXISTS TABLE ') :]}", read=ClickHouseDialect)
            .args["from_"]
            .this
        )
        return {(host, f"{target.db}.{target.name}")}
    tree = parse_one(body, read=ClickHouseDialect)
    source = tree.args.get("from_")
    if source is None:
        return set()
    table = source.this
    hosts = (host,)
    if isinstance(table.this, exp.Anonymous):
        cluster, database, relation, _trailing = cluster_arguments(table.this, "analytics")
        hosts = state.clusters[cluster]
    else:
        database, relation = table.db, table.name
    if (database, relation) != ("system", "tables"):
        return set()
    return {
        (endpoint, f"analytics.{name}")
        for endpoint in hosts
        for name in _filtered_table_names(tree)
    }


def _assert_selected_absence_checks(state, selected):
    waited = set()
    for host, completed_commands, statement in state.query_events:
        if completed_commands:
            resources = _absence_resources(state, host, statement)
            assert resources <= selected, statement
            waited.update(resources)
    assert waited == selected


@pytest.mark.parametrize(
    ("drop_shard", "drop_facade"),
    [(True, True), (True, False), (False, True)],
    ids=["both", "shard-only", "facade-only"],
)
def test_public_drop_keeps_foreign_resources_outside_each_verified_pair_role(
    monkeypatch, write_sql_connections, drop_shard, drop_facade
):
    state = install_drop_scope(monkeypatch, write_sql_connections)
    if not drop_shard:
        foreign = parse_create_table(
            "CREATE TABLE analytics.events (id UInt64) ENGINE=MergeTree ORDER BY id", _TARGET
        )
        state._create(state.remote, foreign, state.foreign_rows)
    before = state.snapshot()
    expected = deepcopy(before)
    selected = set()
    ddl_resources = set()
    if drop_facade:
        expected[state.coordinator].pop(_TARGET)
        selected.add((state.coordinator, _TARGET))
        ddl_resources.add((_TARGET, None))
    if drop_shard:
        expected[state.remote].pop(_PHYSICAL)
        selected.add((state.remote, _PHYSICAL))
        ddl_resources.add((_PHYSICAL, "storage"))

    result = _drop(_TARGET, shard=drop_shard, facade=drop_facade)

    after = state.snapshot()
    diagnostic = {"before": before, "after": after, "commands": state.commands}
    assert after == expected, repr(diagnostic)
    assert result is None
    assert after[state.coordinator][_PHYSICAL] == before[state.coordinator][_PHYSICAL]
    assert state.rows(state.coordinator, _PHYSICAL) == state.foreign_rows
    resources = _drop_resources(state.commands)
    assert set(resources) == ddl_resources
    assert len(resources) == len(ddl_resources)
    assert _drop_resources(state.host_commands[state.coordinator]) == (
        [(_TARGET, None)] if drop_facade else []
    )
    assert _drop_resources(state.host_commands[state.remote]) == (
        [(_PHYSICAL, "storage")] if drop_shard else []
    )
    _assert_selected_absence_checks(state, selected)
    assert state.opened
    assert all(connection.closed for connection in state.opened)
    assert all(connection.close_calls == 1 for connection in state.opened)


@pytest.mark.parametrize("cluster", ["storage", None], ids=["cluster-hint", "local-only"])
def test_public_drop_of_plain_local_table_preserves_an_independent_remote_homonym(
    monkeypatch, write_sql_connections, cluster
):
    state = install_drop_scope(monkeypatch, write_sql_connections)
    target = "analytics.local_events"
    definition = parse_create_table(
        "CREATE TABLE analytics.local_events (id UInt64) ENGINE=MergeTree ORDER BY id", target
    )
    state._create(state.coordinator, definition, [(7,), (8,)])
    state._create(state.remote, definition, state.foreign_rows)
    before = state.snapshot()
    expected = deepcopy(before)
    expected[state.coordinator].pop(target)

    result = _drop(target, cluster=cluster)

    assert result is None
    assert state.snapshot() == expected
    assert state.snapshot()[state.remote][target] == before[state.remote][target]
    assert state.rows(state.remote, target) == state.foreign_rows
    assert state.rows(state.coordinator, _PHYSICAL) == state.foreign_rows
    assert state.rows(state.remote, _PHYSICAL) == [(11,), (12,), (13,)]
    assert _drop_resources(state.commands) == [(target, None)]
    assert state.host_commands[state.remote] == []
    _assert_selected_absence_checks(state, {(state.coordinator, target)})
    assert state.opened
    assert all(connection.closed for connection in state.opened)
    assert all(connection.close_calls == 1 for connection in state.opened)


def _storage_metadata_fault(mode):
    observed = []

    def transform(tree, _rows):
        source = tree.args.get("from_")
        function = source.this.this if source is not None else None
        if not isinstance(function, exp.Anonymous):
            return None
        if function.name.casefold() not in {"cluster", "clusterallreplicas"}:
            return None
        cluster, database, relation, _trailing = cluster_arguments(function, "analytics")
        if cluster != "storage" or database != "system":
            return None
        if mode == "unavailable":
            observed.append(tree.copy())
            message = "Selected storage endpoint is unavailable."
            raise ConnectionError(message)
        fields = tuple(item.name.casefold() for item in tree.expressions)
        if (
            mode == "missing-definition"
            and relation == "tables"
            and fields
            == (
                "_shard_num",
                "create_table_query",
            )
        ):
            observed.append(tree.copy())
            return []
        if (
            mode == "incomplete-host"
            and relation == "one"
            and fields == ("_shard_num", "serveruuid", "tcpport")
        ):
            observed.append(tree.copy())
            return []
        return None

    return transform, observed


def _assert_no_drop_mutation(state, before):
    assert state.snapshot() == before
    assert state.commands == []
    assert all(commands == [] for commands in state.host_commands.values())
    assert all(connection.closed for connection in state.opened)
    assert all(connection.close_calls == 1 for connection in state.opened)


@pytest.mark.parametrize("cluster", ["storage", None], ids=["cluster-hint", "local-only"])
def test_public_facade_only_drop_does_not_require_unrequested_storage(
    monkeypatch, write_sql_connections, cluster
):
    state = install_drop_scope(monkeypatch, write_sql_connections)
    before = state.snapshot()
    state.metadata_transform, storage_queries = _storage_metadata_fault("unavailable")
    expected = deepcopy(before)
    expected[state.coordinator].pop(_TARGET)

    result = _drop(_TARGET, shard=False, cluster=cluster)

    assert result is None
    assert storage_queries == []
    assert state.snapshot() == expected
    assert state.rows(state.remote, _PHYSICAL) == [(11,), (12,), (13,)]
    assert state.rows(state.coordinator, _PHYSICAL) == state.foreign_rows
    assert _drop_resources(state.commands) == [(_TARGET, None)]
    assert state.host_commands[state.remote] == []
    _assert_selected_absence_checks(state, {(state.coordinator, _TARGET)})
    assert state.opened
    assert all(connection.closed for connection in state.opened)
    assert all(connection.close_calls == 1 for connection in state.opened)


@pytest.mark.parametrize("facade", [True, False], ids=["both", "shard-only"])
@pytest.mark.parametrize("fault", ["unavailable", "incomplete-host"])
def test_public_drop_requires_complete_selected_storage_before_any_ddl(
    monkeypatch, write_sql_connections, facade, fault
):
    state = install_drop_scope(monkeypatch, write_sql_connections)
    before = state.snapshot()
    state.metadata_transform, storage_queries = _storage_metadata_fault(fault)

    expected_error = ConnectionError if fault == "unavailable" else InvalidSqlInputError
    with pytest.raises(expected_error):
        _drop(_TARGET, facade=facade)

    assert storage_queries
    _assert_no_drop_mutation(state, before)
    assert state.rows(state.coordinator, _TARGET) == [(11,), (12,), (13,)]
    assert state.rows(state.remote, _PHYSICAL) == [(11,), (12,), (13,)]
    assert state.rows(state.coordinator, _PHYSICAL) == state.foreign_rows


def test_public_drop_of_an_explicit_local_shard_name_does_not_expand_companions(
    monkeypatch, write_sql_connections
):
    state = install_drop_scope(monkeypatch, write_sql_connections)
    companion = "analytics.events_shard_shard"
    definition = parse_create_table(
        "CREATE TABLE analytics.events_shard_shard (id UInt64) ENGINE=MergeTree ORDER BY id",
        companion,
    )
    state._create(state.coordinator, definition, [(99,)])
    before = state.snapshot()
    expected = deepcopy(before)
    expected[state.coordinator].pop(_PHYSICAL)

    result = _drop(_PHYSICAL)

    assert result is None
    assert state.snapshot() == expected
    assert state.rows(state.coordinator, companion) == [(99,)]
    assert state.rows(state.coordinator, _TARGET) == [(11,), (12,), (13,)]
    assert state.rows(state.remote, _PHYSICAL) == [(11,), (12,), (13,)]
    assert _drop_resources(state.commands) == [(_PHYSICAL, None)]
    assert state.host_commands[state.remote] == []
    _assert_selected_absence_checks(state, {(state.coordinator, _PHYSICAL)})
    assert all(connection.closed for connection in state.opened)
    assert all(connection.close_calls == 1 for connection in state.opened)


def test_public_drop_rejects_missing_definition_after_uuid_identification(
    monkeypatch, write_sql_connections
):
    state = install_drop_scope(monkeypatch, write_sql_connections)
    before = state.snapshot()
    state.metadata_transform, missing_definitions = _storage_metadata_fault("missing-definition")

    with pytest.raises(InvalidSqlInputError, match="changed during drop certification"):
        _drop(_TARGET)

    assert missing_definitions
    _assert_no_drop_mutation(state, before)


def test_public_drop_rejects_disabling_both_roles_before_ddl(monkeypatch, write_sql_connections):
    state = install_drop_scope(monkeypatch, write_sql_connections)
    before = state.snapshot()

    with pytest.raises(ValueError, match="At least one"):
        _drop(_TARGET, shard=False, facade=False)

    _assert_no_drop_mutation(state, before)


def test_public_drop_of_an_absent_name_never_claims_suffix_companions(
    monkeypatch, write_sql_connections
):
    state = install_drop_scope(monkeypatch, write_sql_connections)
    missing = "analytics.missing"
    companion = "analytics.missing_shard"
    definition = parse_create_table(
        "CREATE TABLE analytics.missing_shard (id UInt64) ENGINE=MergeTree ORDER BY id", companion
    )
    for host in (state.coordinator, state.remote):
        state._create(host, definition, state.foreign_rows)
    before = state.snapshot()

    result = _drop(missing)

    assert result is None
    _assert_no_drop_mutation(state, before)
    for host in (state.coordinator, state.remote):
        assert state.rows(host, companion) == state.foreign_rows


@pytest.mark.parametrize("facade", [True, False], ids=["both", "shard-only"])
def test_public_drop_with_no_cluster_cannot_remove_a_remote_selected_role(
    monkeypatch, write_sql_connections, facade
):
    state = install_drop_scope(monkeypatch, write_sql_connections)
    before = state.snapshot()

    with pytest.raises(InvalidSqlInputError, match=r"coordinator-local|remote"):
        _drop(_TARGET, facade=facade, cluster=None)

    _assert_no_drop_mutation(state, before)
    assert state.rows(state.coordinator, _TARGET) == [(11,), (12,), (13,)]
    assert state.rows(state.remote, _PHYSICAL) == [(11,), (12,), (13,)]
    assert state.rows(state.coordinator, _PHYSICAL) == state.foreign_rows


def test_public_drop_table_list_certifies_every_target_before_first_drop(
    monkeypatch, write_sql_connections
):
    state = install_drop_scope(monkeypatch, write_sql_connections)
    local = "analytics.local_first"
    definition = parse_create_table(
        "CREATE TABLE analytics.local_first (id UInt64) ENGINE=MergeTree ORDER BY id", local
    )
    state._create(state.coordinator, definition, [(7,), (8,)])
    before = state.snapshot()
    state.metadata_transform, storage_queries = _storage_metadata_fault("unavailable")

    with pytest.raises(ConnectionError, match="Selected storage endpoint is unavailable"):
        _drop([local, _TARGET])

    assert storage_queries
    _assert_no_drop_mutation(state, before)
    assert state.rows(state.coordinator, local) == [(7,), (8,)]
    assert state.rows(state.coordinator, _TARGET) == [(11,), (12,), (13,)]
    assert state.rows(state.remote, _PHYSICAL) == [(11,), (12,), (13,)]
    assert state.rows(state.coordinator, _PHYSICAL) == state.foreign_rows


@pytest.mark.parametrize(
    "tables",
    [
        ["analytics.events", "`analytics`.`events`"],
        ["events", "analytics.events"],
    ],
    ids=["quoted-alias", "qualified-alias"],
)
def test_public_drop_batch_deduplicates_aliases_of_the_same_verified_resources(
    monkeypatch, write_sql_connections, tables
):
    state = install_drop_scope(monkeypatch, write_sql_connections)
    before = state.snapshot()
    expected = deepcopy(before)
    expected[state.coordinator].pop(_TARGET)
    expected[state.remote].pop(_PHYSICAL)

    result = _drop(tables)

    assert result is None
    assert state.snapshot() == expected
    assert state.snapshot()[state.coordinator][_PHYSICAL] == before[state.coordinator][_PHYSICAL]
    assert state.rows(state.coordinator, _PHYSICAL) == state.foreign_rows
    assert _drop_resources(state.commands) == [(_TARGET, None), (_PHYSICAL, "storage")]
    assert _drop_resources(state.host_commands[state.coordinator]) == [(_TARGET, None)]
    assert _drop_resources(state.host_commands[state.remote]) == [(_PHYSICAL, "storage")]
    _assert_selected_absence_checks(
        state, {(state.coordinator, _TARGET), (state.remote, _PHYSICAL)}
    )
    assert state.opened
    assert all(connection.closed for connection in state.opened)
    assert all(connection.close_calls == 1 for connection in state.opened)


def test_public_drop_batch_deduplicates_a_complete_storage_alias_and_explicit_physical_target(
    monkeypatch, write_sql_connections
):
    state = install_drop_scope(monkeypatch, write_sql_connections)
    unrelated = "analytics.unrelated_storage"
    definition = parse_create_table(
        "CREATE TABLE analytics.unrelated_storage (id UInt64) ENGINE=MergeTree ORDER BY id",
        unrelated,
    )
    old_foreign_uuid = state.host_tables[state.coordinator][_PHYSICAL].uuid
    state._create(state.coordinator, definition, list(state.foreign_rows))
    assert state.host_tables[state.coordinator][unrelated].uuid != old_foreign_uuid
    state.host_tables[state.coordinator].pop(_PHYSICAL)
    assert state.clusters["storage_alias"] == state.clusters["storage"] == (state.remote,)
    assert state.rows(state.coordinator, _TARGET) == [(11,), (12,), (13,)]
    assert state.rows(state.remote, _PHYSICAL) == [(11,), (12,), (13,)]
    before = state.snapshot()
    expected = deepcopy(before)
    expected[state.coordinator].pop(_TARGET)
    expected[state.remote].pop(_PHYSICAL)

    result = _drop([_TARGET, _PHYSICAL], cluster="storage_alias")

    assert result is None
    assert state.snapshot() == expected
    assert state.snapshot()[state.coordinator][unrelated] == before[state.coordinator][unrelated]
    assert state.rows(state.coordinator, unrelated) == state.foreign_rows
    assert _drop_resources(state.commands) == [(_TARGET, None), (_PHYSICAL, "storage")]
    assert _drop_resources(state.host_commands[state.coordinator]) == [(_TARGET, None)]
    assert _drop_resources(state.host_commands[state.remote]) == [(_PHYSICAL, "storage")]
    _assert_selected_absence_checks(
        state, {(state.coordinator, _TARGET), (state.remote, _PHYSICAL)}
    )
    assert state.opened
    assert all(connection.closed for connection in state.opened)
    assert all(connection.close_calls == 1 for connection in state.opened)


def test_public_drop_rejects_nil_native_table_uuid_before_any_ddl(
    monkeypatch, write_sql_connections
):
    state = install_drop_scope(monkeypatch, write_sql_connections)
    before = state.snapshot()
    observed = []

    def nil_table_uuid(tree, rows):
        source = tree.args.get("from_")
        function = source.this.this if source is not None else None
        if not isinstance(function, exp.Anonymous):
            return None
        if function.name.casefold() != "clusterallreplicas":
            return None
        cluster, database, relation, _trailing = cluster_arguments(function, "analytics")
        fields = tuple(item.name.casefold() for item in tree.expressions)
        if (cluster, database, relation, fields) != (
            "storage",
            "system",
            "tables",
            ("_shard_num", "uuid"),
        ):
            return None
        assert rows
        observed.append(tree.copy())
        return [(position, "00000000-0000-0000-0000-000000000000") for position, _uuid in rows]

    state.metadata_transform = nil_table_uuid

    with pytest.raises(InvalidSqlInputError, match="UUID") as error:
        _drop(_TARGET)

    assert "nonzero" in str(error.value)
    assert "Atomic" in str(error.value)
    assert observed
    _assert_no_drop_mutation(state, before)
    assert state.rows(state.coordinator, _TARGET) == [(11,), (12,), (13,)]
    assert state.rows(state.remote, _PHYSICAL) == [(11,), (12,), (13,)]
    assert state.rows(state.coordinator, _PHYSICAL) == state.foreign_rows
