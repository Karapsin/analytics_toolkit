from __future__ import annotations

import importlib
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, ClassVar

import pandas as pd
import pytest
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.ch.native_client import _insert_query
from analytics_toolkit.sql.backends.ch.routing import (
    ChClusterRouting,
    _create_target,
    _explicit_cluster,
    _parse_table_name,
    command_local,
    local_sql,
    parse_cluster_routing,
    prepare_plan_sql,
    prepare_sql,
    query_local,
    route_sql,
    routed_connection_sql,
    wrap_client,
)
from analytics_toolkit.sql.connection import get_connection_config
from analytics_toolkit.sql.connection.errors import (
    InvalidSqlInputError,
    SqlConfigError,
    UnsupportedConnectionTypeError,
)
from analytics_toolkit.sql.execution.plans import SqlPlan
from sqlglot import exp

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence
    from pathlib import Path


execute_sql_module = importlib.import_module("analytics_toolkit.sql.dml.io.execute_sql")
read_sql_module = importlib.import_module("analytics_toolkit.sql.dml.io.read_sql")
cancel_queries_module = importlib.import_module("analytics_toolkit.sql.dml.io.cancel_queries")
routing_module = importlib.import_module("analytics_toolkit.sql.backends.ch.routing")
config_module = importlib.import_module("analytics_toolkit.sql.connection.config")


class _Result:
    result_rows: ClassVar[list[tuple[int]]] = [(1,)]
    result_columns: ClassVar[list[tuple[int]]] = [(1,)]
    column_names = ("value",)
    column_types = ("UInt8",)
    row_count = 1


class _Stream:
    def __enter__(self) -> Iterator[pd.DataFrame]:
        return iter([pd.DataFrame({"value": [1]})])

    def __exit__(self, *_args: object) -> None:
        return None


class _InsertContext:
    def __init__(self, table: str) -> None:
        self.table = table
        self.data: Any = None


class _HttpClient:
    def __init__(self) -> None:
        self.commands: list[tuple[str, dict[str, Any]]] = []
        self.queries: list[str] = []
        self.dataframes: list[str] = []
        self.streams: list[str] = []
        self.insert_contexts: list[_InsertContext] = []
        self.query_limit = 100
        self.closed = False

    def command(self, sql: str, **kwargs: Any) -> None:
        self.commands.append((sql, kwargs))

    def query(self, sql: str, **_kwargs: Any) -> Any:
        self.queries.append(sql)
        if "SELECT engine, engine_full, create_table_query" in sql:
            return SimpleNamespace(
                result_rows=[
                    (
                        "MergeTree",
                        "MergeTree()",
                        "CREATE TABLE db.events (id UInt64) ENGINE=MergeTree ORDER BY tuple()",
                    )
                ]
            )
        if "SELECT shard_num, replica_num, host_name" in sql:
            return SimpleNamespace(result_rows=[(1, 1, "host1")])
        if "system, one" in sql:
            return SimpleNamespace(result_rows=[("host1",)])
        if "system, tables" in sql:
            return SimpleNamespace(
                result_rows=[
                    (
                        "host1",
                        "MergeTree",
                        "MergeTree()",
                        "CREATE TABLE db.events (id UInt64) ENGINE=MergeTree ORDER BY tuple()",
                        "uuid1",
                    )
                ]
            )
        return _Result()

    def query_df(self, sql: str, **_kwargs: Any) -> pd.DataFrame:
        self.dataframes.append(sql)
        return pd.DataFrame({"value": [1]})

    def query_df_stream(self, sql: str, **_kwargs: Any) -> _Stream:
        self.streams.append(sql)
        return _Stream()

    def create_insert_context(self, *, table: str, **_kwargs: Any) -> _InsertContext:
        return _InsertContext(table)

    def data_insert(self, context: _InsertContext) -> _InsertContext:
        self.insert_contexts.append(context)
        return context

    def close(self) -> None:
        self.closed = True


class _NativeClient(_HttpClient):
    is_native_transport = True

    def __init__(self) -> None:
        super().__init__()
        self.inserts: list[dict[str, Any]] = []

    def insert(self, **kwargs: Any) -> None:
        self.inserts.append(kwargs)

    def insert_df(
        self,
        table: str,
        df: pd.DataFrame,
        column_names: Sequence[str],
        **kwargs: Any,
    ) -> None:
        self.inserts.append({"table": table, "data": df, "column_names": column_names, **kwargs})


class _SettingsRejectingClient(_HttpClient):
    def command(self, sql: str, **kwargs: Any) -> None:
        if "settings" in kwargs:
            message = "settings are not supported"
            raise TypeError(message)
        super().command(sql, **kwargs)


def _routing(cluster: str = "core") -> ChClusterRouting:
    return ChClusterRouting(cluster=cluster, sharding_key="rand()")


def _config(client_routing: ChClusterRouting | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        cluster_routing=client_routing or _routing(),
        database="default",
    )


def test_clickhouse_connection_parses_optional_cluster_routing(
    write_sql_connections: Callable[[dict[str, dict[str, object]]], Path],
) -> None:
    write_sql_connections(
        {
            "routed": {
                "type": "ch",
                "host": "ch.example",
                "user": "user",
                "password": "password",
                "database": "analytics",
                "cluster_routing": {
                    "cluster": "{cluster}",
                    "sharding_key": "cityHash64(id)",
                },
            }
        }
    )

    config = get_connection_config("routed")

    assert config.cluster_routing == ChClusterRouting("{cluster}", "cityHash64(id)")


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ([], "JSON object"),
        ({}, "cluster_routing.cluster"),
        ({"cluster": " "}, "cluster_routing.cluster"),
        ({"cluster": "core", "unknown": True}, "unsupported field"),
        ({"cluster": "core", "sharding_key": "("}, "valid ClickHouse expression"),
        ({"cluster": "core", "sharding_key": "SELECT 1"}, "single ClickHouse expression"),
    ],
)
def test_cluster_routing_rejects_invalid_config(value: Any, message: str) -> None:
    with pytest.raises(SqlConfigError, match=message):
        parse_cluster_routing(value, "ch")


def test_cluster_routing_none_is_disabled() -> None:
    assert parse_cluster_routing(None, "ch") is None


def test_route_sql_rejects_empty_unparseable_and_partial_parse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(InvalidSqlInputError, match="could not parse"):
        route_sql("", routing=_routing(), database="default")
    with pytest.raises(InvalidSqlInputError, match="could not parse"):
        route_sql("SELECT * FROM (", routing=_routing(), database="default")

    monkeypatch.setattr(routing_module.sqlglot, "parse", lambda *_args, **_kwargs: [None])
    with pytest.raises(InvalidSqlInputError, match="could not parse"):
        route_sql("SELECT 1", routing=_routing(), database="default")
    monkeypatch.setattr(routing_module.sqlglot, "parse", lambda *_args, **_kwargs: [])
    with pytest.raises(InvalidSqlInputError, match="empty SQL"):
        route_sql("SELECT 1", routing=_routing(), database="default")


def test_route_sql_rewrites_nested_named_sources_and_preserves_ctes() -> None:
    sql = (
        "WITH recent AS (SELECT * FROM events.raw) "
        "SELECT * FROM recent r JOIN dimensions.users u USING (user_id)"
    )

    routed = route_sql(sql, routing=_routing(), database="default")

    assert "FROM cluster('core', 'events', 'raw')" in routed
    assert "FROM recent AS r" in routed
    assert "JOIN cluster('core', 'dimensions', 'users') AS u" in routed


def test_route_sql_keeps_table_functions_idempotent_and_routes_named_system_tables() -> None:
    sql = "SELECT * FROM cluster('other', 'db', 'events') e JOIN system.tables s ON 1 = 1"

    routed = route_sql(sql, routing=_routing(), database="default")

    assert routed.count("cluster('other', 'db', 'events')") == 1
    assert "clusterAllReplicas('core', 'system', 'tables') AS s" in routed


def test_explicit_cluster_normalization_skips_unusable_or_unmanaged_functions() -> None:
    class UnmanagedResolver:
        def resolve(self, **_kwargs: Any) -> None:
            return None

    resolver = UnmanagedResolver()
    for sql, database in [
        ("SELECT * FROM cluster('core', 'db')", "db"),
        ("SELECT * FROM cluster('core', tuple(1), tuple(2))", None),
        ("SELECT * FROM cluster('core', 'db', 'events')", "db"),
    ]:
        statement = routing_module.sqlglot.parse_one(sql, read="clickhouse")
        routing_module._normalize_explicit_cluster_tables(
            statement,
            "core",
            database,
            managed_pair_resolver=resolver,
        )

    assert "cluster" in statement.sql(dialect="clickhouse").lower()


def test_explicit_on_cluster_routes_embedded_query_and_wins_over_config() -> None:
    routed = route_sql(
        "CREATE TABLE db.copy ON CLUSTER '{cluster}' AS SELECT * FROM db.source",
        routing=_routing("wrong_cluster"),
        database="default",
    )

    assert "ON CLUSTER '{cluster}'" in routed
    assert "FROM cluster('{cluster}', 'db', 'source')" in routed
    assert "wrong_cluster" not in routed


def test_cluster_override_and_describe_targets_are_routed() -> None:
    overridden = route_sql(
        "SELECT * FROM db.events",
        routing=_routing("configured"),
        database="default",
        cluster_override="override",
    )
    described = route_sql(
        "DESCRIBE TABLE db.events",
        routing=_routing(),
        database="default",
    )
    described_query = route_sql(
        "DESCRIBE TABLE (SELECT * FROM db.events)",
        routing=_routing(),
        database="default",
    )

    assert "cluster('override', 'db', 'events')" in overridden
    assert "cluster('core', 'db', 'events', rand())" in described
    assert "cluster('core', 'db', 'events')" in described_query


@pytest.mark.parametrize(
    "sql",
    ["DESCRIBE SELECT 1", "INSERT INTO FUNCTION null('x') SELECT 1"],
)
def test_route_sql_preserves_statements_without_named_targets(sql: str) -> None:
    assert route_sql(sql, routing=_routing(), database="default") == sql


def test_route_sql_rejects_catalogs_and_conflicting_explicit_clusters() -> None:
    with pytest.raises(InvalidSqlInputError, match="catalog-qualified"):
        route_sql(
            "SELECT * FROM catalog.db.events",
            routing=_routing(),
            database="default",
        )

    statement = exp.Tuple(
        expressions=[
            exp.OnCluster(this=exp.Literal.string("first")),
            exp.OnCluster(this=exp.Literal.string("second")),
        ]
    )
    with pytest.raises(InvalidSqlInputError, match="conflicting ON CLUSTER"):
        _explicit_cluster(statement)


def test_route_sql_injects_cluster_for_ddl_and_routes_insert_target() -> None:
    create = route_sql(
        "CREATE TABLE db.events (id UInt64) ENGINE=MergeTree ORDER BY id",
        routing=_routing(),
        database="default",
    )
    insert = route_sql(
        "INSERT INTO db.events SELECT * FROM db.incoming",
        routing=_routing(),
        database="default",
    )

    assert "CREATE TABLE db.events ON CLUSTER 'core'" in create
    assert "INSERT INTO FUNCTION cluster('core', 'db', 'events', rand())" in insert
    assert "FROM cluster('core', 'db', 'incoming')" in insert


def test_route_sql_injects_cluster_when_create_has_no_properties() -> None:
    routed = route_sql(
        "CREATE DATABASE analytics",
        routing=_routing(),
        database="default",
    )

    assert routed == "CREATE DATABASE analytics ON CLUSTER 'core'"


def test_route_sql_uses_connection_database_and_fails_when_missing() -> None:
    assert (
        route_sql("SELECT * FROM events", routing=_routing(), database="analytics")
        == "SELECT * FROM cluster('core', 'analytics', 'events')"
    )
    with pytest.raises(InvalidSqlInputError, match=r"\.connections\.database"):
        route_sql("SELECT * FROM events", routing=_routing(), database=None)


@pytest.mark.parametrize(
    "sql",
    [
        "OPTIMIZE TABLE db.events FINAL",
        "CREATE TEMPORARY TABLE events (id UInt64)",
    ],
)
def test_route_sql_fails_closed_for_unsafe_statements(sql: str) -> None:
    with pytest.raises(InvalidSqlInputError, match="routing"):
        route_sql(sql, routing=_routing(), database="default")


def test_cluster_routing_client_routes_text_methods_and_delegates_attributes() -> None:
    raw = _HttpClient()
    client = wrap_client(raw, _config())

    client.command("DROP TABLE db.events")
    client.query("SELECT * FROM db.events")
    client.query_df("SELECT * FROM db.events")
    client.query_df_stream("SELECT * FROM db.events")
    assert client.query_limit == 100
    client.query_limit = 0
    client._database = "analytics"
    client.close()

    command, kwargs = raw.commands[0]
    assert "DROP TABLE db.events ON CLUSTER 'core'" in command
    assert kwargs["settings"]["distributed_ddl_task_timeout"] == 0
    assert raw.queries[-1] == "SELECT * FROM cluster('core', 'db', 'events')"
    assert raw.dataframes == ["SELECT * FROM cluster('core', 'db', 'events')"]
    assert raw.streams == ["SELECT * FROM cluster('core', 'db', 'events')"]
    assert raw.query_limit == 0
    assert raw.closed is True


def test_cluster_routing_client_retries_command_without_unsupported_settings() -> None:
    raw = _SettingsRejectingClient()
    client = wrap_client(raw, _config())

    client.command("DROP TABLE db.events")

    assert raw.commands == [("DROP TABLE db.events ON CLUSTER 'core'", {})]


def test_local_sql_bypasses_routing_for_control_queries() -> None:
    raw = _HttpClient()
    client = wrap_client(raw, _config())

    query_local(client, "SELECT * FROM system.clusters")
    command_local(client, "CREATE TABLE db.local (id UInt8) ENGINE=Memory")

    assert raw.queries == ["SELECT * FROM system.clusters"]
    assert raw.commands[0][0] == "CREATE TABLE db.local (id UInt8) ENGINE=Memory"


def test_cluster_routing_client_routes_http_and_native_binary_inserts() -> None:
    dataframe = pd.DataFrame({"id": [1, 2]})
    http_raw = _HttpClient()
    http = wrap_client(http_raw, _config())
    native_raw = _NativeClient()
    native = wrap_client(native_raw, _config())

    http.insert("db.events", [(1,), (2,)], ["id"])
    http.insert_df("db.events", dataframe, ["id"])
    native.insert(
        "db.events",
        [(1,), (2,)],
        ["id"],
        column_type_names=["UInt64"],
    )
    native.insert_df("db.events", dataframe, ["id"])

    assert http_raw.insert_contexts[0].table == ("FUNCTION cluster('core', 'db', 'events', rand())")
    assert http_raw.insert_contexts[0].data == [(1,), (2,)]
    assert http_raw.insert_contexts[1].table == ("FUNCTION cluster('core', 'db', 'events', rand())")
    assert http_raw.insert_contexts[1].data is dataframe
    assert native_raw.inserts[0]["table"] == ("FUNCTION cluster('core', 'db', 'events', rand())")
    assert native_raw.inserts[1]["table"] == ("FUNCTION cluster('core', 'db', 'events', rand())")


def test_cluster_routing_client_keeps_local_binary_inserts_local() -> None:
    dataframe = pd.DataFrame({"id": [1]})
    raw = _NativeClient()
    client = wrap_client(raw, _config())

    with local_sql(client):
        client.insert("db.events", [(1,)], ["id"])
        client.insert_df("db.events", dataframe, ["id"])

    assert raw.inserts[0]["table"] == "db.events"
    assert raw.inserts[1]["table"] == "db.events"


def test_routed_http_insert_rejects_unknown_options() -> None:
    client = wrap_client(_HttpClient(), _config())

    with pytest.raises(TypeError, match="Unsupported routed ClickHouse insert option"):
        client.insert_df("db.events", pd.DataFrame({"id": [1]}), ["id"], unexpected=True)


def test_disabled_routing_helpers_return_original_objects_and_sql() -> None:
    raw = _HttpClient()
    config = _config()
    config.cluster_routing = None
    adapter = get_backend_adapter("ch")

    assert wrap_client(raw, config) is raw
    assert prepare_sql(adapter, config, "SELECT * FROM events") == "SELECT * FROM events"
    assert routed_connection_sql(raw, "SELECT * FROM events") == "SELECT * FROM events"


@pytest.mark.parametrize("function_name", ["cluster", "clusterAllReplicas"])
def test_native_insert_query_accepts_routed_table_function(function_name: str) -> None:
    table = f"FUNCTION {function_name}('core', 'db', 'events', rand())"

    assert _insert_query(table, ["id"]) == f"INSERT INTO {table} (`id`) VALUES"


def test_execute_sql_dry_run_contains_routed_sql(
    write_sql_connections: Callable[[dict[str, dict[str, object]]], Path],
) -> None:
    write_sql_connections(
        {
            "routed": {
                "type": "ch",
                "host": "ch.example",
                "user": "user",
                "password": "password",
                "database": "analytics",
                "cluster_routing": {"cluster": "core"},
            }
        }
    )

    plan = execute_sql_module.execute_sql(
        "routed",
        "SELECT * FROM events",
        dry_run=True,
    )

    assert plan.sqls == ["SELECT * FROM cluster('core', 'analytics', 'events')"]


def test_generated_plan_sql_is_routed_and_keeps_local_create_fallback(
    write_sql_connections: Callable[[dict[str, dict[str, object]]], Path],
) -> None:
    write_sql_connections(
        {
            "routed": {
                "type": "ch",
                "host": "ch.example",
                "user": "user",
                "password": "password",
                "database": "analytics",
                "cluster_routing": {"cluster": "core"},
            }
        }
    )
    plan = SqlPlan(operation="generated")
    plan.add(
        "CREATE TABLE analytics.events (id UInt8) ENGINE=Memory",
        alias="routed",
        backend="ch",
    )
    plan.add(
        "CREATE TABLE analytics.shard ON CLUSTER '{cluster}' (id UInt8) ENGINE=Memory",
        alias="routed",
        backend="ch",
    )
    plan.add(
        "CREATE TABLE IF NOT EXISTS analytics.shard (id UInt8) ENGINE=Memory",
        alias="routed",
        backend="ch",
    )
    plan.add(
        "INSERT INTO analytics.events VALUES (1)",
        alias="routed",
        backend="ch",
    )

    assert "ON CLUSTER 'core'" in plan.sqls[0]
    assert "ON CLUSTER '{cluster}'" in plan.sqls[1]
    assert "ON CLUSTER" not in plan.sqls[2]
    assert plan.sqls[3] == (
        "INSERT INTO FUNCTION cluster('core', 'analytics', 'events', rand()) VALUES ((1))"
    )
    assert (
        prepare_plan_sql(
            get_backend_adapter("ch"),
            "routed",
            "INSERT INTO analytics.events VALUES <1 dataframe row>",
            [],
        )
        == "INSERT INTO analytics.events VALUES <1 dataframe row>"
    )


def test_plan_without_connection_metadata_keeps_sql_unchanged() -> None:
    plan = SqlPlan(operation="local")

    plan.add("SELECT 1")

    assert plan.sqls == ["SELECT 1"]


def test_prepare_plan_sql_ignores_unsupported_connection_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _unsupported(_key: str) -> None:
        message = "unsupported"
        raise UnsupportedConnectionTypeError(message)

    monkeypatch.setattr(config_module, "get_connection_config", _unsupported)

    assert prepare_plan_sql(get_backend_adapter("ch"), "unsupported", "SELECT 1", []) == "SELECT 1"


def test_read_sql_routes_with_live_connection_metadata(
    monkeypatch: pytest.MonkeyPatch,
    write_sql_connections: Callable[[dict[str, dict[str, object]]], Path],
) -> None:
    write_sql_connections(
        {
            "routed": {
                "type": "ch",
                "host": "ch.example",
                "user": "user",
                "password": "password",
                "database": "analytics",
                "cluster_routing": {"cluster": "core"},
            }
        }
    )
    raw = _HttpClient()
    routed = wrap_client(raw, _config())
    routed._database = "analytics"
    monkeypatch.setattr(read_sql_module, "get_sql_connection", lambda _key: routed)

    result = read_sql_module.read_sql("routed", "SELECT * FROM events")

    assert list(result["value"]) == [1]
    assert raw.dataframes == ["SELECT * FROM cluster('core', 'analytics', 'events')"]


def test_cancel_query_stays_local_with_cluster_routing(
    monkeypatch: pytest.MonkeyPatch,
    write_sql_connections: Callable[[dict[str, dict[str, object]]], Path],
) -> None:
    write_sql_connections(
        {
            "routed": {
                "type": "ch",
                "host": "ch.example",
                "user": "user",
                "password": "password",
                "database": "analytics",
                "cluster_routing": {"cluster": "core"},
            }
        }
    )
    raw = _HttpClient()
    monkeypatch.setattr(read_sql_module, "get_sql_connection", lambda _key: raw)

    cancel_queries_module.cancel_queries("routed", ["query-id"])

    assert raw.dataframes == ["KILL QUERY WHERE query_id = 'query-id' SYNC"]


def test_generated_create_commands_preserve_intentional_local_fallback() -> None:
    raw = _HttpClient()
    connection = wrap_client(raw, _config())
    adapter = get_backend_adapter("ch")

    adapter.execute_commands(
        connection,
        [
            "CREATE TABLE db.events ON CLUSTER '{cluster}' (id UInt8) ENGINE=Memory",
            "CREATE TABLE IF NOT EXISTS db.events (id UInt8) ENGINE=Memory",
        ],
    )

    assert "ON CLUSTER '{cluster}'" in raw.commands[0][0]
    assert "ON CLUSTER" not in raw.commands[1][0]


def test_clickhouse_source_estimate_explains_routed_source_locally() -> None:
    raw = _HttpClient()
    connection = wrap_client(raw, _config())

    estimate = get_backend_adapter("ch").estimate_source_rows(
        connection,
        "SELECT value FROM events",
    )

    assert estimate == 1
    assert raw.queries[-1] == (
        "EXPLAIN ESTIMATE SELECT value FROM cluster('core', 'default', 'events')"
    )


@pytest.mark.parametrize("sql", ["SELECT 1", "SELECT * FROM ("])
def test_create_target_returns_no_target_for_non_create_sql(sql: str) -> None:
    assert _create_target(sql) == (None, False)


def test_create_target_returns_no_target_for_non_table_create(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        routing_module.sqlglot,
        "parse_one",
        lambda *_args, **_kwargs: exp.Create(this=exp.Var(this="target")),
    )

    assert _create_target("CREATE SOMETHING") == (None, False)


@pytest.mark.parametrize("table", ["(", "numbers(1)"])
def test_parse_table_name_rejects_invalid_targets(table: str) -> None:
    with pytest.raises(InvalidSqlInputError, match="Invalid ClickHouse table name"):
        _parse_table_name(table)
