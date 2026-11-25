"""Malformed metadata, static identities, and scope collisions fail safely."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.ch import source_schema
from analytics_toolkit.sql.backends.ch.cluster_functions import cluster_names
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from analytics_toolkit.sql.backends.ch.managed_routing import _ClusterObject, _normalized_definition
from analytics_toolkit.sql.backends.ch.outage_client import OutageClient
from analytics_toolkit.sql.backends.ch.outage_context import logical_statistics
from analytics_toolkit.sql.backends.ch.outage_session import OutageSession
from analytics_toolkit.sql.backends.ch.routing import (
    ChClusterRouting,
    _identifier_value,
    route_sql,
    wrap_client,
)
from analytics_toolkit.sql.backends.models import SourceColumn
from analytics_toolkit.sql.connection.config import get_connection_config
from analytics_toolkit.sql.execution.backend_options import BackendOperationState, backend_operation
from sqlglot import exp, parse_one


@pytest.mark.parametrize("identity", ["42", "'db.('"])
def test_invalid_two_argument_identity_never_becomes_a_routed_source(identity: str) -> None:
    with pytest.raises(sql.ClickHouseClusterTopologyError, match="static"):
        route_sql(
            f"SELECT * FROM cluster('core', {identity})",
            routing=ChClusterRouting("core"),
            database="db",
        )


def test_preflight_cluster_inventory_supports_qualified_identity_and_rejects_dynamic_cluster() -> (
    None
):
    assert cluster_names(
        parse_one("SELECT * FROM cluster(core, 'db.events')", read=ClickHouseDialect)
    ) == ["core"]
    with pytest.raises(sql.ClickHouseClusterTopologyError, match="static cluster"):
        cluster_names(
            parse_one(
                "SELECT * FROM cluster(concat('co','re'), 'db.events')", read=ClickHouseDialect
            )
        )


@pytest.mark.parametrize("definition", ["CREATE TABLE (", "", "RENAME TABLE db.first TO db.second"])
def test_invalid_ddl_metadata_is_never_certified_as_a_consistent_schema(definition: str) -> None:
    metadata = _ClusterObject("host", "MergeTree", "", definition, "")
    with pytest.raises(sql.ClickHouseClusterTopologyError, match=r"compare.*safely"):
        _normalized_definition(metadata)


def test_replica_expression_changes_are_not_erased_as_permitted_replica_name_differences() -> None:
    first = (
        "CREATE TABLE db.events (id UInt64) "
        "ENGINE=ReplicatedMergeTree('/path', getMacro('first')) ORDER BY id"
    )
    second = first.replace("getMacro('first')", "getMacro('second')")
    assert _normalized_definition(
        _ClusterObject("a", "ReplicatedMergeTree", "", first, "")
    ) != _normalized_definition(_ClusterObject("b", "ReplicatedMergeTree", "", second, ""))


def test_identifier_compatibility_keeps_column_names_exact() -> None:
    assert _identifier_value(exp.column(" core ", quoted=True)) == " core "


def test_non_mutating_command_does_not_receive_distributed_ddl_settings() -> None:
    recorded = []
    raw = SimpleNamespace(command=lambda query, **kwargs: recorded.append((query, kwargs)))
    connection = wrap_client(
        raw, SimpleNamespace(cluster_routing=ChClusterRouting("core"), database="db")
    )
    connection.command("SELECT 1", settings={"max_threads": 2})
    assert recorded == [("SELECT 1", {"settings": {"max_threads": 2}})]


def test_generated_alias_never_shadows_an_existing_user_alias() -> None:
    query = (
        "SELECT db.events.id FROM db.events JOIN db.lookup AS _atk_source_1 ON 1=1 "
        "WHERE EXISTS (SELECT 1 FROM other.events WHERE other.events.id=db.events.id)"
    )
    tree = parse_one(
        route_sql(query, routing=ChClusterRouting("core"), database="db"), read=ClickHouseDialect
    )
    aliases = [table.alias for table in tree.find_all(exp.Table)]
    assert aliases.count("_atk_source_1") == 1
    assert len(aliases) == len(set(aliases))
    inner = tree.find(exp.Exists).this
    assert inner.args["where"].this.right.table == tree.args["from_"].this.alias


def test_internal_statistics_preflight_checks_only_explicit_remote_sources(outage: Any) -> None:
    connection = sql.get_sql_connection("ch")
    state = BackendOperationState()
    session = OutageSession(connection._client, get_connection_config("ch"), state)
    query = parse_one(
        "SELECT t.name FROM system.clusters c CROSS JOIN "
        "clusterAllReplicas('core','system','tables') t",
        read=ClickHouseDialect,
    )
    with logical_statistics():
        assert session.infer_routing(query) is None
        assert session.infer_routing(parse_one("SELECT 1", read=ClickHouseDialect)) is None
    assert state.statuses
    assert all(status["coverage_intent"] == "physical_hosts" for status in state.statuses.values())


def test_programmatic_insert_function_ast_preserves_named_columns(
    outage: Any, monkeypatch: Any
) -> None:
    recorded = []
    with backend_operation() as state:
        connection = sql.get_sql_connection("ch")
        monkeypatch.setattr(
            connection._client, "command", lambda query, **_kwargs: recorded.append(query)
        )
        statement = parse_one("INSERT INTO db.events (id) VALUES (42)", read=ClickHouseDialect)
        function = parse_one(
            "SELECT * FROM cluster('core','db','events',rand())", read=ClickHouseDialect
        ).find(exp.Table)
        statement.this.set("this", function)
        statement.set("is_function", True)
        connection._execute_statement(
            "command",
            "INSERT INTO db.events (id) VALUES (42)",
            {},
            state=state,
            session=connection._get_session(state),
            statement=statement,
            special=None,
        )
    assert len(recorded) == 1
    assert "events_shard" in recorded[0]
    assert "(id)" in recorded[0]
    assert "42" in recorded[0]


def test_non_assignment_extension_setting_is_ignored_without_relaxing_required_settings() -> None:
    statement = exp.select("1")
    statement.set("settings", [exp.Var(this="extension_option")])
    OutageClient._check_inline_settings(statement, {"async_insert": 0})
    statement.set(
        "settings", [exp.EQ(this=exp.column("async_insert"), expression=exp.Literal.number(1))]
    )
    with pytest.raises(sql.ClickHouseClusterTopologyError, match="contradicts"):
        OutageClient._check_inline_settings(statement, {"async_insert": 0})


def test_unknown_same_backend_metadata_uses_the_empty_family_default() -> None:
    adapter = get_backend_adapter("ch")
    assert (
        source_schema.map_same_backend_source_type_to_target(adapter, SourceColumn("value", None))
        == "Nullable(String)"
    )


@pytest.mark.parametrize(
    ("family", "native", "expected"),
    [("integer", "uint256", "UInt256"), ("integer", "int128", "Int128"), ("uuid", "uuid", "UUID")],
)
def test_exported_source_mapping_helpers_preserve_exact_native_families(
    family: str, native: str, expected: str
) -> None:
    assert source_schema._map_to_ch_base_type(family, native, None, None) == expected
