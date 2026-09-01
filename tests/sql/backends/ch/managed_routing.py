from __future__ import annotations

# ruff: noqa: C901, EM101, TRY003
from types import SimpleNamespace
from typing import Any, ClassVar, Sequence

import pandas as pd
import pytest
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.ch.managed_routing import (
    ManagedPairResolver,
    _query_count,
    _resolve_cluster_name,
    _strip_sql_wrapping_quotes,
)
from analytics_toolkit.sql.backends.ch.routing import (
    ChClusterRouting,
    prepare_sql,
    wrap_client,
)
from analytics_toolkit.sql.connection.errors import ClickHouseClusterTopologyError


class _Result:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.result_rows = rows


class _InsertContext:
    def __init__(self, table: str) -> None:
        self.table = table
        self.data: Any = None


class _Client:
    engine = "Distributed"
    engine_full = "Distributed('core', 'db', 'events_shard', rand())"
    visible_tables = 2

    def __init__(self) -> None:
        self.queries: list[str] = []
        self.dataframes: list[str] = []
        self.commands: list[str] = []
        self.context_tables: list[str] = []
        self.insert_contexts: list[_InsertContext] = []
        self.visible_facades = 2
        self.fail_coverage = False
        self.fail_metadata = False
        self.inconsistent_facade = False
        self.metadata_rows: list[tuple[Any, ...]] | None = None
        self.macro_rows: list[tuple[Any, ...]] = [("core",)]

    def query(self, sql: str, **_kwargs: Any) -> _Result:  # noqa: PLR0911 - fake dispatch
        self.queries.append(sql)
        if "SELECT engine, engine_full" in sql:
            if self.fail_metadata:
                message = "metadata unavailable"
                raise RuntimeError(message)
            if self.metadata_rows is not None:
                return _Result(self.metadata_rows)
            return _Result(
                [
                    (
                        self.engine,
                        self.engine_full,
                        f"CREATE TABLE db.events (id UInt64) ENGINE={self.engine_full}",
                    )
                ]
            )
        if "SELECT getMacro" in sql:
            return _Result(self.macro_rows)
        if "SELECT shard_num, replica_num, host_name" in sql:
            if self.fail_coverage:
                message = "topology unavailable"
                raise RuntimeError(message)
            return _Result([(1, 1, "host1"), (1, 2, "host2")])
        if "system, one" in sql:
            if self.fail_coverage:
                raise RuntimeError("topology unavailable")
            return _Result([("host1",), ("host2",)])
        if "system, replicas" in sql:
            return _Result(
                [
                    ("host1", "/path/events", "replica1", 0, 0),
                    ("host2", "/path/events", "replica2", 0, 0),
                ][: self.visible_tables]
            )
        if "system, tables" in sql:
            physical = "name = 'events_shard'" in sql
            count = self.visible_tables if physical else self.visible_facades
            engine = "ReplicatedMergeTree" if physical else self.engine
            engine_full = (
                "ReplicatedMergeTree('/path/events', '{replica}')" if physical else self.engine_full
            )
            relation = "events_shard" if physical else "events"
            ddl = f"CREATE TABLE db.{relation} (id UInt64) ENGINE={engine_full}"
            return _Result(
                [
                    (
                        f"host{replica}",
                        engine,
                        engine_full,
                        (
                            f"{ddl} SETTINGS index_granularity = {replica}"
                            if self.inconsistent_facade and not physical
                            else ddl
                        ),
                        f"uuid{replica}",
                    )
                    for replica in range(1, count + 1)
                ]
            )
        return _Result([(1,)])

    def query_df(self, sql: str, **_kwargs: Any) -> pd.DataFrame:
        self.dataframes.append(sql)
        return pd.DataFrame({"id": [1]})

    def command(self, sql: str, **_kwargs: Any) -> None:
        self.commands.append(sql)

    def create_insert_context(self, *, table: str, **_kwargs: Any) -> _InsertContext:
        self.context_tables.append(table)
        return _InsertContext(table)

    def data_insert(self, context: _InsertContext) -> _InsertContext:
        self.insert_contexts.append(context)
        return context


class _NativeClient(_Client):
    is_native_transport: ClassVar[bool] = True

    def __init__(self) -> None:
        super().__init__()
        self.inserts: list[dict[str, Any]] = []

    def insert(
        self,
        table: str,
        data: Sequence[Sequence[Any]],
        column_names: Sequence[str],
        **kwargs: Any,
    ) -> None:
        self.inserts.append({"table": table, "data": data, "column_names": column_names, **kwargs})

    def insert_df(
        self,
        table: str,
        df: pd.DataFrame,
        column_names: Sequence[str],
        **kwargs: Any,
    ) -> None:
        self.inserts.append({"table": table, "data": df, "column_names": column_names, **kwargs})


def _config() -> SimpleNamespace:
    return SimpleNamespace(
        cluster_routing=ChClusterRouting("core", "rand()"),
        database="db",
    )


def test_full_coverage_routes_named_reads_and_text_inserts_to_physical_shard() -> None:
    raw = _Client()
    client = wrap_client(raw, _config())

    client.query_df("SELECT * FROM events")
    client.command("INSERT INTO events (id) VALUES (2)")

    assert raw.dataframes == ["SELECT * FROM cluster('core', 'db', 'events_shard')"]
    assert "FUNCTION cluster('core', 'db', 'events_shard', rand())" in raw.commands[0]
    assert "rand()) (id) VALUES" in raw.commands[0]
    assert sum("SELECT engine, engine_full" in sql for sql in raw.queries) == 1


def test_incomplete_coverage_uses_local_distributed_facade() -> None:
    raw = _Client()
    raw.visible_tables = 1
    client = wrap_client(raw, _config())

    client.query_df("SELECT * FROM db.events")
    client.command("INSERT INTO db.events (id) VALUES (2)")

    assert raw.dataframes == ["SELECT * FROM db.events"]
    assert raw.commands[0].startswith("INSERT INTO db.events")
    assert "cluster(" not in raw.commands[0]


def test_incomplete_distributed_facade_uses_local_facade() -> None:
    raw = _Client()
    raw.visible_facades = 1
    client = wrap_client(raw, _config())

    client.query_df("SELECT * FROM db.events")
    client.command("INSERT INTO db.events (id) VALUES (2)")

    assert raw.dataframes == ["SELECT * FROM db.events"]
    assert raw.commands[0].startswith("INSERT INTO db.events")


def test_inconsistent_distributed_facade_uses_local_facade() -> None:
    raw = _Client()
    raw.inconsistent_facade = True

    wrapped = wrap_client(raw, _config())
    wrapped.query_df("SELECT * FROM db.events")

    assert raw.dataframes == ["SELECT * FROM db.events"]


def test_failed_coverage_probe_fails_closed() -> None:
    raw = _Client()
    raw.fail_coverage = True
    client = wrap_client(raw, _config())

    with pytest.raises(ClickHouseClusterTopologyError, match="inspect ClickHouse cluster"):
        client.query_df("SELECT * FROM db.events")


def test_confirmed_missing_physical_shard_uses_local_facade() -> None:
    raw = _Client()
    raw.visible_tables = 0
    client = wrap_client(raw, _config())

    client.query_df("SELECT * FROM db.events")

    assert raw.dataframes == ["SELECT * FROM db.events"]


def test_other_distributed_facade_stays_local() -> None:
    raw = _Client()
    raw.engine_full = "Distributed('core', 'db', 'other_shard', rand())"
    client = wrap_client(raw, _config())

    client.query_df("SELECT * FROM db.events")

    assert raw.dataframes == ["SELECT * FROM db.events"]


@pytest.mark.parametrize(
    ("engine", "engine_full"),
    [
        ("MergeTree", "MergeTree()"),
        ("Distributed", "Distributed('core', 'db')"),
        ("Distributed", "Distributed('core', 'other', 'events_shard', rand())"),
    ],
)
def test_nonstandard_objects_use_engine_dependent_routing(
    engine: str,
    engine_full: str,
) -> None:
    raw = _Client()
    raw.engine = engine
    raw.engine_full = engine_full
    client = wrap_client(raw, _config())

    client.query_df("SELECT * FROM db.events")

    expected = (
        "SELECT * FROM clusterAllReplicas('core', 'db', 'events')"
        if engine == "MergeTree"
        else "SELECT * FROM db.events"
    )
    assert raw.dataframes == [expected]


@pytest.mark.parametrize(
    "metadata_rows",
    [[], [("Distributed",)], [(1, 2)]],
)
def test_incomplete_facade_metadata_fails_closed(
    metadata_rows: list[tuple[Any, ...]],
) -> None:
    raw = _Client()
    raw.metadata_rows = metadata_rows
    client = wrap_client(raw, _config())

    with pytest.raises(ClickHouseClusterTopologyError):
        client.query_df("SELECT * FROM db.events")


def test_failed_facade_metadata_fails_closed() -> None:
    raw = _Client()
    raw.fail_metadata = True
    client = wrap_client(raw, _config())

    with pytest.raises(ClickHouseClusterTopologyError, match="Could not inspect"):
        client.query_df("SELECT * FROM db.events")


def test_explicit_table_functions_are_normalized_and_system_uses_all_replicas() -> None:
    raw = _Client()
    client = wrap_client(raw, _config())

    explicit = client.route("SELECT * FROM cluster('core', 'db', 'events')")
    system = client.route("SELECT * FROM system.tables")

    assert explicit == "SELECT * FROM cluster('core', 'db', 'events_shard')"
    assert system == "SELECT * FROM clusterAllReplicas('core', 'system', 'tables')"
    assert raw.queries


def test_http_and_native_binary_inserts_use_physical_shard() -> None:
    dataframe = pd.DataFrame({"id": [1]})
    http_raw = _Client()
    http = wrap_client(http_raw, _config())
    native_raw = _NativeClient()
    native = wrap_client(native_raw, _config())

    http.insert("db.events", [(1,)], ["id"])
    http.insert_df("db.events", dataframe, ["id"])
    native.insert("db.events", [(1,)], ["id"], column_type_names=["UInt64"])
    native.insert_df("db.events", dataframe, ["id"])

    assert http_raw.context_tables == ["db.events_shard", "db.events_shard"]
    assert all(
        context.table == "FUNCTION cluster('core', 'db', 'events_shard', rand())"
        for context in http_raw.insert_contexts
    )
    assert all(
        insert["table"] == "FUNCTION cluster('core', 'db', 'events_shard', rand())"
        for insert in native_raw.inserts
    )


def test_binary_inserts_use_local_facade_when_coverage_is_incomplete() -> None:
    raw = _Client()
    raw.visible_tables = 1
    client = wrap_client(raw, _config())

    client.insert("db.events", [(1,)], ["id"])
    client.insert_df("db.events", pd.DataFrame({"id": [1]}), ["id"])

    assert raw.context_tables == ["db.events", "db.events"]
    assert all(context.table == "db.events" for context in raw.insert_contexts)
    assert all(context.data is not None for context in raw.insert_contexts)


def test_explicit_cluster_function_falls_back_to_local_distributed_facade() -> None:
    raw = _Client()
    raw.visible_tables = 1
    client = wrap_client(raw, _config())

    routed = client.route("SELECT * FROM cluster('core', 'db', 'events')")

    assert routed == "SELECT * FROM db.events"


def test_table_ddl_invalidates_managed_pair_resolution() -> None:
    raw = _Client()
    client = wrap_client(raw, _config())

    client.query_df("SELECT * FROM db.events")
    raw.visible_tables = 1
    client.command("ALTER TABLE db.events ADD COLUMN payload String")
    client.query_df("SELECT * FROM db.events")

    assert raw.dataframes == [
        "SELECT * FROM cluster('core', 'db', 'events_shard')",
        "SELECT * FROM db.events",
    ]
    assert sum("SELECT engine, engine_full" in sql for sql in raw.queries) == 2


def test_prepare_sql_validates_but_defers_live_routing() -> None:
    config = _config()

    prepared = prepare_sql(
        get_backend_adapter("ch"),
        config,
        "SELECT * FROM events",
    )

    assert prepared == "SELECT * FROM events"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("core", "core"),
        ("'co''re'", "co're"),
        ('"core"', "core"),
        ("`core`", "core"),
        ("'unclosed", "'unclosed"),
    ],
)
def test_cluster_quote_normalization(value: str, expected: str) -> None:
    assert _strip_sql_wrapping_quotes(value) == expected


def test_cluster_macro_resolution() -> None:
    raw = _Client()

    assert _resolve_cluster_name(raw, "'{cluster}'") == "core"


@pytest.mark.parametrize("macro_rows", [[], [(None,)], [(" ",)]])
def test_invalid_cluster_macro_resolution_fails(
    macro_rows: list[tuple[Any, ...]],
) -> None:
    raw = _Client()
    raw.macro_rows = macro_rows

    with pytest.raises(ValueError, match="Could not resolve"):
        _resolve_cluster_name(raw, "{cluster}")


def test_empty_count_result_is_zero() -> None:
    raw = _Client()
    raw.metadata_rows = []

    assert _query_count(raw, "SELECT engine, engine_full FROM system.tables") == 0
    assert _query_count(raw, "SELECT count()") == 1


def test_view_relations_always_stay_local() -> None:
    raw = _Client()
    raw.engine = "View"
    raw.engine_full = "View"

    route = ManagedPairResolver(raw).resolve(cluster="core", database="db", table="events")

    assert route.mode == "local"


def test_local_relation_metadata_rejects_invalid_values() -> None:
    raw = _Client()
    raw.metadata_rows = [("MergeTree", 1, "CREATE TABLE db.events")]

    with pytest.raises(ClickHouseClusterTopologyError, match="invalid values"):
        ManagedPairResolver(raw)._read_local_object(database="db", table="events")


@pytest.mark.parametrize("rows", [[(1,)], [], [(1, 1, "a"), (1, 1, "b")]])
def test_cluster_host_metadata_requires_complete_unique_hosts(
    monkeypatch: pytest.MonkeyPatch,
    rows: list[tuple[Any, ...]],
) -> None:
    raw = _Client()
    monkeypatch.setattr(raw, "query", lambda _sql: _Result(rows))

    with pytest.raises(ClickHouseClusterTopologyError):
        ManagedPairResolver(raw)._cluster_hosts("core")


def test_cluster_host_probe_wraps_query_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = _Client()
    monkeypatch.setattr(
        raw,
        "query",
        lambda _sql: (_ for _ in ()).throw(OSError("metadata unavailable")),
    )

    with pytest.raises(ClickHouseClusterTopologyError, match="inspect ClickHouse cluster"):
        ManagedPairResolver(raw)._cluster_hosts("core")


def test_cluster_object_validation_rejects_missing_and_inconsistent_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = _Client()
    resolver = ManagedPairResolver(raw)
    monkeypatch.setattr(resolver, "_cluster_hosts", lambda _cluster: {1: 2})

    monkeypatch.setattr(raw, "query", lambda _sql: _Result([(1,)]))
    with pytest.raises(ClickHouseClusterTopologyError, match=r"metadata.*incomplete"):
        resolver._read_consistent_cluster_object(cluster="core", database="db", table="events")

    inconsistent = [
        ("host1", "MergeTree", "MergeTree()", "CREATE TABLE a", "uuid1"),
        ("host2", "MergeTree", "MergeTree()", "CREATE TABLE b", "uuid2"),
    ]
    monkeypatch.setattr(raw, "query", lambda _sql: _Result(inconsistent))
    with pytest.raises(ClickHouseClusterTopologyError, match="inconsistent engine or DDL"):
        resolver._read_consistent_cluster_object(cluster="core", database="db", table="events")


def test_cluster_object_probe_wraps_failure_and_incomplete_coverage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = _Client()
    resolver = ManagedPairResolver(raw)
    monkeypatch.setattr(resolver, "_cluster_hosts", lambda _cluster: {1: 2})
    monkeypatch.setattr(
        raw,
        "query",
        lambda _sql: (_ for _ in ()).throw(OSError("cluster query failed")),
    )
    with pytest.raises(ClickHouseClusterTopologyError, match="every host"):
        resolver._read_consistent_cluster_object(cluster="core", database="db", table="events")

    monkeypatch.setattr(
        raw,
        "query",
        lambda _sql: _Result([("host1", "MergeTree", "MergeTree()", "CREATE TABLE a", "uuid1")]),
    )
    with pytest.raises(ClickHouseClusterTopologyError, match="1/2"):
        resolver._read_consistent_cluster_object(cluster="core", database="db", table="events")


@pytest.mark.parametrize(
    "rows",
    [
        [(1,)],
        [("host1", "/path", "same", 0, 0), ("host2", "/path", "same", 0, 0)],
        [("host1", "/a", "one", 0, 0), ("host2", "/b", "two", 0, 0)],
    ],
)
def test_replica_validation_rejects_incomplete_unhealthy_or_mismatched_rows(
    monkeypatch: pytest.MonkeyPatch,
    rows: list[tuple[Any, ...]],
) -> None:
    raw = _Client()
    monkeypatch.setattr(raw, "query", lambda _sql: _Result(rows))

    with pytest.raises(ClickHouseClusterTopologyError):
        ManagedPairResolver(raw)._validate_replicas(
            cluster_name="core",
            database="db",
            table="events",
            expected={1: 2},
        )


def test_replica_and_reachability_query_failures_are_wrapped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = _Client()
    resolver = ManagedPairResolver(raw)
    monkeypatch.setattr(
        raw,
        "query",
        lambda _sql: (_ for _ in ()).throw(OSError("host unavailable")),
    )
    with pytest.raises(ClickHouseClusterTopologyError, match="validate replicas"):
        resolver._validate_replicas(
            cluster_name="core",
            database="db",
            table="events",
            expected={1: 1},
        )

    monkeypatch.setattr(resolver, "_cluster_hosts", lambda _cluster: {1: 2})
    with pytest.raises(ClickHouseClusterTopologyError, match="reach every host"):
        resolver._validate_cluster_reachable("core")

    monkeypatch.setattr(raw, "query", lambda _sql: _Result([("host1",)]))
    with pytest.raises(ClickHouseClusterTopologyError, match="Only 1/2"):
        resolver._validate_cluster_reachable("core")


def test_route_mode_rejects_unknown_replicated_engine() -> None:
    with pytest.raises(ClickHouseClusterTopologyError, match="does not know how"):
        ManagedPairResolver._route_mode("Memory", {1: 2})


def test_cluster_probes_use_supported_hostname_metadata() -> None:
    raw = _Client()

    ManagedPairResolver(raw).resolve(cluster="core", database="db", table="events")

    cluster_queries = [query for query in raw.queries if "clusterAllReplicas" in query]
    assert cluster_queries
    assert all("_replica_num" not in query for query in cluster_queries)
    assert all("_shard_num" not in query and "hostName()" in query for query in cluster_queries)
