from __future__ import annotations

from tests.sql._support.adapters import (
    Any,
    FakeClickHouseResult,
    RecordingClickHouseClient,
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
    with pytest.raises(RuntimeError, match="close failed"):
        ch_lifecycle_backend_module._drop_ch_distributed_table_pair_on_host(
            "host-a",
            pair=pair,
            query_label=None,
            per_host_connection_factory=lambda _host: ClosingConnection(),
        )


def test_clickhouse_lifecycle_retries_all_hosts_when_leftover_host_unmapped() -> None:
    class RootClient(RecordingClickHouseClient):
        def __init__(self) -> None:
            super().__init__()
            self.table_queries = 0

        def query(self, sql: str) -> FakeClickHouseResult:
            self.queries.append(sql)
            if sql.startswith("SELECT getMacro("):
                return FakeClickHouseResult([("core",)])
            if "clusterAllReplicas" in sql and "system, one" in sql:
                return FakeClickHouseResult([(2,)])
            if sql.startswith("SELECT count()") and "FROM system.clusters" in sql:
                return FakeClickHouseResult([(2,)])
            if sql.startswith("SELECT DISTINCT host_name"):
                return FakeClickHouseResult([("host-a",), ("host-b",)])
            if "clusterAllReplicas" in sql and "system, tables" in sql:
                self.table_queries += 1
                if self.table_queries <= 2:
                    return FakeClickHouseResult(
                        [
                            (
                                "clickhouse-01",
                                "db",
                                "target_shard",
                                "ReplicatedMergeTree",
                            )
                        ]
                    )
                return FakeClickHouseResult([])
            return FakeClickHouseResult([])

    class HostClient(RecordingClickHouseClient):
        def __init__(self, host: str) -> None:
            super().__init__()
            self.host = host

    root_client = RootClient()
    host_clients: dict[str, HostClient] = {}

    def host_factory(host: str) -> HostClient:
        host_client = HostClient(host)
        host_clients[host] = host_client
        return host_client

    ch_lifecycle_module.drop_ch_distributed_table_pair(
        root_client,
        "db.target",
        ch_cluster="{cluster}",
        wait_for_absence=True,
        wait_timeout_seconds=0,
        wait_poll_interval_seconds=0,
        ch_retry_per_host_drops=True,
        per_host_connection_factory=host_factory,
    )

    assert set(host_clients) == {"host-a", "host-b"}
    for host_client in host_clients.values():
        assert host_client.commands == [
            ("DROP TABLE IF EXISTS db.target", None),
            ("DROP TABLE IF EXISTS db.target_shard", None),
        ]
    assert root_client.table_queries == 3


def test_clickhouse_lifecycle_retries_drop_on_cluster_hosts() -> None:
    class RootClient(RecordingClickHouseClient):
        def __init__(self) -> None:
            super().__init__()
            self.table_queries = 0

        def query(self, sql: str) -> FakeClickHouseResult:
            self.queries.append(sql)
            if sql.startswith("SELECT getMacro("):
                return FakeClickHouseResult([("core",)])
            if "clusterAllReplicas" in sql and "system, one" in sql:
                return FakeClickHouseResult([(2,)])
            if sql.startswith("SELECT count()") and "FROM system.clusters" in sql:
                return FakeClickHouseResult([(2,)])
            if sql.startswith("SELECT DISTINCT host_name"):
                return FakeClickHouseResult([("host-a",), ("host-b",)])
            if "clusterAllReplicas" in sql and "system, tables" in sql:
                self.table_queries += 1
                if self.table_queries <= 2:
                    return FakeClickHouseResult(
                        [("host-b", "db", "target_shard", "ReplicatedMergeTree")]
                    )
                return FakeClickHouseResult([])
            return FakeClickHouseResult([])

    class HostClient(RecordingClickHouseClient):
        def __init__(self, host: str) -> None:
            super().__init__()
            self.host = host
            self.close_calls = 0

        def close(self) -> None:
            self.close_calls += 1

    root_client = RootClient()
    host_clients: dict[str, HostClient] = {}

    def host_factory(host: str) -> HostClient:
        host_client = HostClient(host)
        host_clients[host] = host_client
        return host_client

    ch_lifecycle_module.drop_ch_distributed_table_pair(
        root_client,
        "db.target",
        ch_cluster="{cluster}",
        wait_for_absence=True,
        wait_timeout_seconds=0,
        wait_poll_interval_seconds=0,
        ch_retry_per_host_drops=True,
        per_host_connection_factory=host_factory,
    )

    assert set(host_clients) == {"host-b"}
    assert host_clients["host-b"].commands == [
        ("DROP TABLE IF EXISTS db.target", None),
        ("DROP TABLE IF EXISTS db.target_shard", None),
    ]
    assert host_clients["host-b"].close_calls == 1
    assert root_client.table_queries == 3


def test_clickhouse_lifecycle_retries_per_host_drops_concurrently() -> None:
    class RootClient(RecordingClickHouseClient):
        def __init__(self) -> None:
            super().__init__()
            self.table_queries = 0

        def query(self, sql: str) -> FakeClickHouseResult:
            self.queries.append(sql)
            if sql.startswith("SELECT getMacro("):
                return FakeClickHouseResult([("core",)])
            if "clusterAllReplicas" in sql and "system, one" in sql:
                return FakeClickHouseResult([(2,)])
            if sql.startswith("SELECT count()") and "FROM system.clusters" in sql:
                return FakeClickHouseResult([(2,)])
            if sql.startswith("SELECT DISTINCT host_name"):
                return FakeClickHouseResult([("host-a",), ("host-b",)])
            if "clusterAllReplicas" in sql and "system, tables" in sql:
                self.table_queries += 1
                if self.table_queries <= 2:
                    return FakeClickHouseResult(
                        [
                            ("host-a", "db", "target_shard", "ReplicatedMergeTree"),
                            ("host-b", "db", "target_shard", "ReplicatedMergeTree"),
                        ]
                    )
                return FakeClickHouseResult([])
            return FakeClickHouseResult([])

    active = 0
    max_active = 0
    active_lock = threading.Lock()
    barrier = threading.Barrier(2, timeout=5)

    class HostClient(RecordingClickHouseClient):
        def command(
            self,
            sql: str,
            settings: dict[str, object] | None = None,
        ) -> dict[str, int] | None:
            nonlocal active, max_active
            if sql == "DROP TABLE IF EXISTS db.target":
                with active_lock:
                    active += 1
                    max_active = max(max_active, active)
                try:
                    barrier.wait()
                finally:
                    with active_lock:
                        active -= 1
            return super().command(sql, settings=settings)

    host_clients: dict[str, HostClient] = {}

    def host_factory(host: str) -> HostClient:
        host_client = HostClient()
        host_clients[host] = host_client
        return host_client

    ch_lifecycle_module.drop_ch_distributed_table_pair(
        RootClient(),
        "db.target",
        ch_cluster="{cluster}",
        wait_for_absence=True,
        wait_timeout_seconds=0,
        wait_poll_interval_seconds=0,
        ch_retry_per_host_drops=True,
        per_host_connection_factory=host_factory,
    )

    assert set(host_clients) == {"host-a", "host-b"}
    assert max_active == 2


def test_clickhouse_lifecycle_timeout_requirements(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ch_lifecycle_backend_module, "_execute_ch_sqls", lambda *_: None)
    monkeypatch.setattr(
        ch_lifecycle_backend_module,
        "_wait_for_ch_distributed_table_pair_absence",
        lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError("lagging")),
    )
    with pytest.raises(TimeoutError, match="lagging"):
        ch_lifecycle_backend_module.drop_ch_distributed_table_pair(
            object(), "db.t", wait_for_absence=True, ch_retry_per_host_drops=False
        )
    with pytest.raises(TimeoutError, match="non-null ch_cluster"):
        ch_lifecycle_backend_module.drop_ch_distributed_table_pair(
            object(),
            "db.t",
            ch_cluster=None,
            wait_for_absence=True,
            ch_retry_per_host_drops=True,
        )


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
