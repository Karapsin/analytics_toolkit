from __future__ import annotations

from copy import deepcopy

from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from analytics_toolkit.sql.backends.ch.reconfigure_ddl import parse_create_table
from sqlglot import exp, parse_one

from tests.sql._support.adapters import (
    Any,
    SimpleNamespace,
    ch_backend_wait_module,
    ch_lifecycle_backend_module,
    ch_lifecycle_module,
    ch_target_create_backend_module,
    ch_upsert_backend_module,
    pd,
    pytest,
    threading,
)
from tests.sql._support.assertions import strip_query_label
from tests.sql._support.ch_drop_scope import _DropConnection, install_drop_scope
from tests.sql._support.reconfigure_conversion_scope import _name, _scope_sql


def test_clickhouse_lifecycle_host_selection_and_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pair = ch_lifecycle_backend_module.ch_distributed_table_pair("db.t")
    assert (
        ch_lifecycle_backend_module._select_ch_hosts_for_local_drop(
            object(), pair, ch_cluster="core", configured_hosts=[]
        )
        == []
    )
    monkeypatch.setattr(
        ch_lifecycle_backend_module,
        "_resolve_ch_cluster_name_for_wait",
        lambda _connection, cluster: cluster,
    )
    monkeypatch.setattr(
        ch_lifecycle_backend_module,
        "_query_ch_cluster_table_rows",
        lambda *args, **kwargs: [],
    )
    assert ch_lifecycle_backend_module._select_ch_hosts_for_local_drop(
        object(), pair, ch_cluster="core", configured_hosts=["a", "b"]
    ) == ["a", "b"]
    monkeypatch.setattr(
        ch_lifecycle_backend_module,
        "_query_ch_cluster_table_rows",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("query failed")),
    )
    assert ch_lifecycle_backend_module._select_ch_hosts_for_local_drop(
        object(), pair, ch_cluster="core", configured_hosts=["a"]
    ) == ["a"]

    class Result:
        def __init__(self) -> None:
            self.result_rows = [(), ("  ",), (" host-a ",)]

    class Connection:
        def query(self, _sql: str) -> Result:
            return Result()

    assert ch_lifecycle_backend_module._query_ch_configured_cluster_hosts(Connection(), "core") == [
        "host-a"
    ]
    monkeypatch.setattr(
        ch_lifecycle_backend_module,
        "_query_ch_configured_cluster_hosts",
        lambda *_: [],
    )
    with pytest.raises(TimeoutError, match="could not find any configured"):
        ch_lifecycle_backend_module._drop_ch_distributed_table_pair_on_cluster_hosts(
            object(),
            pair,
            ch_cluster="core",
            query_label=None,
            per_host_drop_workers=1,
            per_host_connection_factory=lambda _host: object(),
        )


def test_clickhouse_lifecycle_per_host_error_and_close_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pair = ch_lifecycle_backend_module.ch_distributed_table_pair("db.t")
    factory_error = RuntimeError("boom")

    def fail_factory(_host: str) -> Any:
        raise factory_error

    assert "boom" in ch_lifecycle_backend_module._drop_ch_distributed_table_pair_on_host(
        "host-a",
        pair=pair,
        query_label=None,
        per_host_connection_factory=fail_factory,
    )

    close_error = RuntimeError("close failed")

    class ClosingConnection:
        def close(self) -> None:
            raise close_error

    monkeypatch.setattr(ch_lifecycle_backend_module, "_execute_ch_sqls", lambda *_: None)
    assert (
        ch_lifecycle_backend_module._drop_ch_distributed_table_pair_on_host(
            "host-a",
            pair=pair,
            query_label=None,
            per_host_connection_factory=lambda _host: None,
        )
        is None
    )
    assert (
        ch_lifecycle_backend_module._drop_ch_distributed_table_pair_on_host(
            "host-a",
            pair=pair,
            query_label=None,
            per_host_connection_factory=lambda _host: object(),
        )
        is None
    )
    assert (
        ch_lifecycle_backend_module._drop_ch_distributed_table_pair_on_host(
            "host-a",
            pair=pair,
            query_label=None,
            per_host_connection_factory=lambda _host: ClosingConnection(),
        )
        is None
    )


_LIFECYCLE_TARGET = "analytics.events"
_LIFECYCLE_PHYSICAL = "analytics.events_shard"


class _LifecycleDropClient(_DropConnection):
    def __init__(self, state: Any, host: str) -> None:
        super().__init__(state, host, native=True)
        self.commands: list[tuple[str, Any]] = []

    def command(self, statement: str, settings: Any = None) -> Any:
        self.commands.append((statement, settings))
        cluster, body = _scope_sql(strip_query_label(statement))
        if cluster is None and self.state.before_local_drop is not None:
            tree = parse_one(body, read=ClickHouseDialect)
            if isinstance(tree, exp.Drop):
                targets = tree.args.get("tables") or [tree.this]
                assert len(targets) == 1
                if _name(targets[0]) == _LIFECYCLE_PHYSICAL:
                    self.state.before_local_drop()
        return super().command(statement, settings=settings)

    def _delay_drop(self, statement: str, settings: Any) -> bool:
        cluster, body = _scope_sql(strip_query_label(statement))
        tree = parse_one(body, read=ClickHouseDialect)
        if not isinstance(tree, exp.Drop):
            return False
        targets = tree.args.get("tables") or [tree.this]
        assert len(targets) == 1
        name = _name(targets[0])
        if cluster is None and (self.host, name) in self.state.pending_local_drops:
            self.state.commands.append(statement)
            self.state.command_settings.append(deepcopy(settings))
            self.state.host_commands[self.host].append(statement)
            return True
        if not super()._delay_drop(statement, settings):
            return False
        for host in self.state.clusters[cluster]:
            if self.state.host_endpoints[host][0] not in self.state.pending_drop_hosts:
                self.state.host_tables[host].pop(name, None)
        return True


def _lifecycle_drop_fixture(
    monkeypatch: Any, write_sql_connections: Any, *, pending: set[str]
) -> tuple[Any, _LifecycleDropClient, dict[str, _LifecycleDropClient], Any]:
    state = install_drop_scope(monkeypatch, write_sql_connections, driver="native")
    remote = "host-b"
    state.host_tables[remote] = {}
    state.host_commands[remote] = []
    state.host_endpoints[state.remote] = ("host-a", "10.0.0.2", 9000)
    state.host_endpoints[remote] = (remote, "10.0.0.3", 9000)
    state.server_identities[remote] = ("00000000-0000-0000-0000-000000000003", 9000)
    physical = state.host_tables[state.remote][_LIFECYCLE_PHYSICAL]
    state._create(remote, physical.create.copy(), deepcopy(physical.rows))
    state.clusters["storage"] = (state.remote, remote)
    state.pending_drop_hosts = pending
    state.pending_local_drops = set()
    state.before_local_drop = None
    state.delay_cluster_drops = {("storage", _LIFECYCLE_PHYSICAL)}
    root = _LifecycleDropClient(state, state.coordinator)
    clients: dict[str, _LifecycleDropClient] = {}

    def host_factory(endpoint: Any) -> _LifecycleDropClient:
        matches = [
            host
            for host, (name, address, port) in state.host_endpoints.items()
            if str(endpoint) in {host, name, address} and endpoint.tcp_port == port
        ]
        assert len(matches) == 1
        host = matches[0]
        client = _LifecycleDropClient(state, host)
        clients[state.host_endpoints[host][0]] = client
        return client

    return state, root, clients, host_factory


def _assert_lifecycle_recovered(state: Any, before: Any, clients: Any) -> None:
    expected = deepcopy(before)
    expected[state.coordinator].pop(_LIFECYCLE_TARGET)
    for host in state.clusters["storage"]:
        expected[host].pop(_LIFECYCLE_PHYSICAL)
    assert state.snapshot() == expected
    assert state.delayed_cluster_drops == [("storage", _LIFECYCLE_PHYSICAL)]
    assert state.rows(state.coordinator, _LIFECYCLE_PHYSICAL) == state.foreign_rows
    for client in clients.values():
        assert len(client.commands) == 1
        statement, settings = client.commands[0]
        cluster, body = _scope_sql(strip_query_label(statement))
        assert cluster is None
        tree = parse_one(body, read=ClickHouseDialect)
        assert isinstance(tree, exp.Drop)
        targets = tree.args.get("tables") or [tree.this]
        assert len(targets) == 1
        assert _name(targets[0]) == _LIFECYCLE_PHYSICAL
        assert settings is None
        assert client.closed
        assert client.close_calls == 1
    assert state.query_settings
    assert all(settings == {"skip_unavailable_shards": 0} for settings in state.query_settings)


def test_clickhouse_lifecycle_retries_all_hosts_when_leftover_host_unmapped(
    monkeypatch: pytest.MonkeyPatch, write_sql_connections: Any
) -> None:
    state, root, clients, host_factory = _lifecycle_drop_fixture(
        monkeypatch, write_sql_connections, pending={"host-a", "host-b"}
    )
    original_host_record = state._host_record
    monkeypatch.setattr(
        state,
        "_host_record",
        lambda host: {**original_host_record(host), "hostName": "clickhouse-01"},
    )
    before = state.snapshot()

    ch_lifecycle_module.drop_ch_distributed_table_pair(
        root,
        _LIFECYCLE_TARGET,
        ch_cluster="storage",
        wait_for_absence=True,
        wait_timeout_seconds=0,
        wait_poll_interval_seconds=0,
        ch_retry_per_host_drops=True,
        per_host_connection_factory=host_factory,
    )

    assert set(clients) == {"host-a", "host-b"}
    _assert_lifecycle_recovered(state, before, clients)


def test_clickhouse_lifecycle_retries_drop_on_cluster_hosts(
    monkeypatch: pytest.MonkeyPatch, write_sql_connections: Any
) -> None:
    state, root, clients, host_factory = _lifecycle_drop_fixture(
        monkeypatch, write_sql_connections, pending={"host-b"}
    )
    before = state.snapshot()

    ch_lifecycle_module.drop_ch_distributed_table_pair(
        root,
        _LIFECYCLE_TARGET,
        ch_cluster="storage",
        wait_for_absence=True,
        wait_timeout_seconds=0,
        wait_poll_interval_seconds=0,
        ch_retry_per_host_drops=True,
        per_host_connection_factory=host_factory,
    )

    assert set(clients) == {"host-b"}
    _assert_lifecycle_recovered(state, before, clients)


def test_clickhouse_lifecycle_retries_per_host_drops_concurrently(
    monkeypatch: pytest.MonkeyPatch, write_sql_connections: Any
) -> None:
    state, root, clients, host_factory = _lifecycle_drop_fixture(
        monkeypatch, write_sql_connections, pending={"host-a", "host-b"}
    )
    before = state.snapshot()
    active = 0
    max_active = 0
    active_lock = threading.Lock()
    barrier = threading.Barrier(2, timeout=5)

    def before_local_drop() -> None:
        nonlocal active, max_active
        with active_lock:
            active += 1
            max_active = max(max_active, active)
        try:
            barrier.wait()
        finally:
            with active_lock:
                active -= 1

    state.before_local_drop = before_local_drop

    ch_lifecycle_module.drop_ch_distributed_table_pair(
        root,
        _LIFECYCLE_TARGET,
        ch_cluster="storage",
        wait_for_absence=True,
        wait_timeout_seconds=0,
        wait_poll_interval_seconds=0,
        ch_retry_per_host_drops=True,
        per_host_connection_factory=host_factory,
    )

    assert set(clients) == {"host-a", "host-b"}
    assert max_active == 2
    _assert_lifecycle_recovered(state, before, clients)


def test_clickhouse_lifecycle_timeout_requirements(
    monkeypatch: pytest.MonkeyPatch, write_sql_connections: Any
) -> None:
    state, root, clients, host_factory = _lifecycle_drop_fixture(
        monkeypatch, write_sql_connections, pending={"host-a", "host-b"}
    )
    before = state.snapshot()
    with pytest.raises(TimeoutError, match="remains visible"):
        ch_lifecycle_backend_module.drop_ch_distributed_table_pair(
            root,
            _LIFECYCLE_TARGET,
            ch_cluster="storage",
            wait_for_absence=True,
            wait_timeout_seconds=0,
            wait_poll_interval_seconds=0,
            ch_retry_per_host_drops=False,
            per_host_connection_factory=host_factory,
        )
    assert clients == {}
    for host in state.clusters["storage"]:
        assert state.snapshot()[host] == before[host]
    assert (
        state.snapshot()[state.coordinator][_LIFECYCLE_PHYSICAL]
        == before[state.coordinator][_LIFECYCLE_PHYSICAL]
    )

    local = "analytics.local_target"
    state._create(
        state.coordinator,
        parse_create_table(f"CREATE TABLE {local} (id UInt64) ENGINE=MergeTree ORDER BY id", local),
        [(1,)],
    )
    state.pending_local_drops.add((state.coordinator, local))
    before = state.snapshot()
    with pytest.raises(TimeoutError, match="remains visible"):
        ch_lifecycle_backend_module.drop_ch_distributed_table_pair(
            root,
            local,
            ch_cluster=None,
            wait_for_absence=True,
            wait_timeout_seconds=0,
            wait_poll_interval_seconds=0,
            ch_retry_per_host_drops=True,
            per_host_connection_factory=host_factory,
        )
    assert state.snapshot() == before
    assert clients == {}


def test_clickhouse_target_explicit_type_validation() -> None:
    batch = pd.DataFrame({"a": [1], "b": [2]})
    adapter = SimpleNamespace(infer_dataframe_column_type=lambda _series: "UInt64")
    with pytest.raises(ValueError, match="Missing explicit SQL type for column 'b'"):
        ch_target_create_backend_module.expected_create_table_column_types(
            adapter,
            batch,
            {"a": "UInt8"},
            ch_distributed_table=True,
            ch_only_shard=False,
        )
    with pytest.raises(ValueError, match="must not be empty"):
        ch_target_create_backend_module.expected_create_table_column_types(
            adapter,
            pd.DataFrame({"a": [1]}),
            {"a": "  "},
            ch_distributed_table=True,
            ch_only_shard=False,
        )


def test_clickhouse_upsert_requirements_and_placeholder_sqls() -> None:
    adapter = SimpleNamespace(
        build_preserved_target_rows_insert_sql=lambda *args, **kwargs: "preserved",
        build_incoming_rows_insert_sql=lambda *args, **kwargs: "incoming",
        build_drop_upsert_partition_sqls=lambda *args, **kwargs: ["drop"],
        build_insert_from_stage_placeholder_sql=lambda *args, **kwargs: "final",
    )
    with pytest.raises(ValueError, match="are required"):
        ch_upsert_backend_module.build_upsert_stage_sqls(
            adapter,
            "target",
            "stage",
            columns=["id"],
            key_columns=["id"],
        )
    with pytest.raises(ValueError, match="are required"):
        ch_upsert_backend_module.build_upsert_stage_placeholder_sqls(
            adapter, "target", "stage", key_columns=["id"]
        )
    assert ch_upsert_backend_module.build_upsert_stage_placeholder_sqls(
        adapter,
        "target",
        "stage",
        key_columns=["id"],
        upsert_partition_column="month",
        final_stage_table="final_stage",
    ) == ["preserved", "incoming", "drop", "final"]


def test_clickhouse_wait_cluster_absence_wrapper_and_plain_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        ch_backend_wait_module,
        "_wait_for_ch_tables_absence_on_cluster",
        lambda _connection, names, **_kwargs: calls.append(names),
    )
    ch_backend_wait_module._wait_for_ch_table_absence_on_cluster(
        object(), "db.t", ch_cluster="core"
    )
    assert calls == [["db.t"]]

    monkeypatch.setattr(
        ch_backend_wait_module,
        "_query_ch_expected_cluster_hosts",
        lambda *args, **kwargs: 1,
    )
    monkeypatch.setattr(ch_backend_wait_module, "_query_ch_count", lambda *args: 0)
    monkeypatch.setattr(ch_backend_wait_module.time, "monotonic", lambda: 1.0)
    monkeypatch.setattr(
        ch_backend_wait_module,
        "_describe_ch_cluster_schema_mismatch",
        lambda *args, **kwargs: "",
    )
    with pytest.raises(TimeoutError, match="did not match expected") as exc_info:
        ch_backend_wait_module._wait_for_ch_table_schema_on_cluster(
            object(),
            "db.t",
            expected_column_types={"id": "UInt64"},
            ch_cluster="core",
            timeout_seconds=0,
            poll_interval_seconds=0,
        )
    assert exc_info.value.__cause__ is None


def test_clickhouse_wait_eventual_and_timeout_cause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Result:
        def __init__(self, rows: list[tuple[object, ...]]) -> None:
            self.result_rows = rows

    class SequenceConnection:
        def __init__(self, rows: list[list[tuple[object, ...]]]) -> None:
            self.rows = iter(rows)

        def query(self, _sql: str) -> Result:
            return Result(next(self.rows))

    ticks = iter([0.0, 0.1, 0.2, 0.3])
    sleeps: list[float] = []
    monkeypatch.setattr(ch_backend_wait_module.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(ch_backend_wait_module.time, "sleep", sleeps.append)
    ch_backend_wait_module._wait_for_ch_table(
        SequenceConnection([[(0,)], [(1,)]]),
        "db.t",
        timeout_seconds=1,
        poll_interval_seconds=0.25,
    )
    assert sleeps == [0.25]

    ticks = iter([0.0, 0.1, 0.2, 0.3])
    sleeps.clear()
    ch_backend_wait_module._wait_for_ch_table_absence(
        SequenceConnection([[(1,)], [(0,)]]),
        "db.t",
        timeout_seconds=1,
        poll_interval_seconds=0.5,
    )
    assert sleeps == [0.5]

    monkeypatch.setattr(
        ch_backend_wait_module,
        "_query_ch_expected_cluster_hosts",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("schema query")),
    )
    monkeypatch.setattr(ch_backend_wait_module.time, "monotonic", lambda: 1.0)
    monkeypatch.setattr(
        ch_backend_wait_module,
        "_describe_ch_cluster_schema_mismatch",
        lambda *args, **kwargs: "details",
    )
    with pytest.raises(TimeoutError, match="details") as exc_info:
        ch_backend_wait_module._wait_for_ch_table_schema_on_cluster(
            object(),
            "db.t",
            expected_column_types={"id": "UInt64"},
            ch_cluster="core",
            timeout_seconds=0,
            poll_interval_seconds=0,
        )
    assert isinstance(exc_info.value.__cause__, RuntimeError)


def test_clickhouse_wait_schema_diagnostics_and_macro_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed = (
        [("extra", "String", 1)] + [(f"c{i}", "Wrong", 1) for i in range(7)] + [("malformed",)]
    )
    monkeypatch.setattr(
        ch_backend_wait_module,
        "_query_ch_rows",
        lambda *_: observed,
    )
    details = ch_backend_wait_module._describe_ch_cluster_schema_mismatch(
        object(),
        "db.t",
        expected_column_types={f"c{i}": "UInt64" for i in range(7)},
        ch_cluster="core",
        expected_hosts=2,
    )
    assert "Schema mismatch details" in details
    assert details.endswith("...")

    monkeypatch.setattr(
        ch_backend_wait_module,
        "_query_ch_rows",
        lambda *_: [("amount", "Decimal(18, 4)", 1)],
    )
    equivalent_details = ch_backend_wait_module._describe_ch_cluster_schema_mismatch(
        object(),
        "db.t",
        expected_column_types={"amount": "Decimal(18,4)"},
        ch_cluster="core",
        expected_hosts=1,
    )
    assert equivalent_details == ""

    class EmptyMacroConnection:
        def query(self, _sql: str) -> Any:
            return SimpleNamespace(result_rows=[])

    with pytest.raises(ValueError, match="Could not resolve"):
        ch_backend_wait_module._resolve_ch_cluster_name_for_wait(
            EmptyMacroConnection(), "{cluster}"
        )

    macro_error = RuntimeError("macro failed")

    class FailingMacroConnection:
        def query(self, _sql: str) -> Any:
            raise macro_error

    with pytest.raises(ValueError, match="Could not resolve") as exc_info:
        ch_backend_wait_module._resolve_ch_cluster_name_for_wait(
            FailingMacroConnection(), "{cluster}"
        )
    assert isinstance(exc_info.value.__cause__, RuntimeError)
