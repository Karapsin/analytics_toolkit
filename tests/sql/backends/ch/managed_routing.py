from __future__ import annotations

from types import SimpleNamespace
from typing import Any, ClassVar, Sequence

import pandas as pd
import pytest
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.ch.managed_routing import (
    _query_count,
    _resolve_cluster_name,
    _strip_sql_wrapping_quotes,
)
from analytics_toolkit.sql.backends.ch.routing import (
    ChClusterRouting,
    prepare_sql,
    wrap_client,
)


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
    configured_hosts = 2
    reachable_hosts = 2
    visible_tables = 2

    def __init__(self) -> None:
        self.queries: list[str] = []
        self.dataframes: list[str] = []
        self.commands: list[str] = []
        self.context_tables: list[str] = []
        self.insert_contexts: list[_InsertContext] = []
        self.fail_coverage = False
        self.fail_metadata = False
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
            return _Result([(self.engine, self.engine_full)])
        if "SELECT getMacro" in sql:
            return _Result(self.macro_rows)
        if "FROM system.clusters" in sql:
            if self.fail_coverage:
                message = "topology unavailable"
                raise RuntimeError(message)
            return _Result([(self.configured_hosts,)])
        if "system, one" in sql:
            return _Result([(self.reachable_hosts,)])
        if "system, tables" in sql:
            return _Result([(self.visible_tables,)])
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


def test_failed_coverage_probe_uses_local_distributed_facade() -> None:
    raw = _Client()
    raw.fail_coverage = True
    client = wrap_client(raw, _config())

    client.query_df("SELECT * FROM db.events")

    assert raw.dataframes == ["SELECT * FROM db.events"]


def test_empty_routing_cluster_uses_local_distributed_facade() -> None:
    raw = _Client()
    raw.configured_hosts = 0
    raw.reachable_hosts = 0
    raw.visible_tables = 0
    client = wrap_client(raw, _config())

    client.query_df("SELECT * FROM db.events")

    assert raw.dataframes == ["SELECT * FROM db.events"]


def test_nonmanaged_tables_keep_normal_cluster_routing() -> None:
    raw = _Client()
    raw.engine_full = "Distributed('core', 'db', 'other_shard', rand())"
    client = wrap_client(raw, _config())

    client.query_df("SELECT * FROM db.events")

    assert raw.dataframes == ["SELECT * FROM cluster('core', 'db', 'events')"]


@pytest.mark.parametrize(
    ("engine", "engine_full"),
    [
        ("MergeTree", "MergeTree()"),
        ("Distributed", "Distributed('core', 'db')"),
        ("Distributed", "Distributed('core', 'other', 'events_shard', rand())"),
    ],
)
def test_nonstandard_facades_keep_normal_cluster_routing(
    engine: str,
    engine_full: str,
) -> None:
    raw = _Client()
    raw.engine = engine
    raw.engine_full = engine_full
    client = wrap_client(raw, _config())

    client.query_df("SELECT * FROM db.events")

    assert raw.dataframes == ["SELECT * FROM cluster('core', 'db', 'events')"]


@pytest.mark.parametrize(
    "metadata_rows",
    [[], [("Distributed",)], [(1, 2)]],
)
def test_incomplete_facade_metadata_keeps_normal_cluster_routing(
    metadata_rows: list[tuple[Any, ...]],
) -> None:
    raw = _Client()
    raw.metadata_rows = metadata_rows
    client = wrap_client(raw, _config())

    client.query_df("SELECT * FROM db.events")

    assert raw.dataframes == ["SELECT * FROM cluster('core', 'db', 'events')"]


def test_failed_facade_metadata_keeps_normal_cluster_routing() -> None:
    raw = _Client()
    raw.fail_metadata = True
    client = wrap_client(raw, _config())

    client.query_df("SELECT * FROM db.events")

    assert raw.dataframes == ["SELECT * FROM cluster('core', 'db', 'events')"]


def test_explicit_table_functions_and_system_tables_are_not_resolved() -> None:
    raw = _Client()
    client = wrap_client(raw, _config())

    explicit = client.route("SELECT * FROM cluster('core', 'db', 'events')")
    system = client.route("SELECT * FROM system.tables")

    assert explicit == "SELECT * FROM cluster('core', 'db', 'events')"
    assert system == "SELECT * FROM cluster('core', 'system', 'tables')"
    assert raw.queries == []


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

    assert raw.context_tables == ["db.events"]
    assert raw.insert_contexts[0].table == "db.events"


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
