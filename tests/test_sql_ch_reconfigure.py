from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.ch.reconfigure import (
    ChReconfigureOptions,
    plan_ch_table_reconfiguration,
)
from analytics_toolkit.sql.connection.errors import (
    InvalidSqlInputError,
    UnsupportedConnectionTypeError,
)
from analytics_toolkit.sql.execution.plans import SqlOperationResult, SqlPlan
from sqlglot import exp
from tests.sql_fakes import FakeClickHouseResult

reconfigure_api = importlib.import_module("analytics_toolkit.sql.dml.table.ch_reconfigure")
reconfigure_backend = importlib.import_module("analytics_toolkit.sql.backends.ch.reconfigure")
reconfigure_ddl = importlib.import_module("analytics_toolkit.sql.backends.ch.reconfigure_ddl")
reconfigure_execution = importlib.import_module(
    "analytics_toolkit.sql.backends.ch.reconfigure_execution"
)


TABLE_DDL = """
CREATE TABLE analytics.events
(
    `id` UInt64,
    `dt` Date,
    INDEX idx_id id TYPE minmax GRANULARITY 1
)
ENGINE = Distributed('{cluster}', 'analytics', 'events_shard', rand())
""".strip()

SHARD_DDL = """
CREATE TABLE analytics.events_shard
(
    `id` UInt64,
    `dt` Date,
    INDEX idx_id id TYPE minmax GRANULARITY 1
)
ENGINE = ReplicatedMergeTree('/clickhouse/{table}', '{replica}')
PARTITION BY toYYYYMM(dt)
ORDER BY (dt, id)
SETTINGS index_granularity = 8192
""".strip()

LOCAL_DDL = """
CREATE TABLE analytics.local_events
(
    `id` UInt64,
    `dt` Date
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(dt)
ORDER BY (dt, id)
""".strip()


class ReconfigureClient:
    def __init__(
        self,
        *,
        database_engine: str = "Atomic",
        source_hosts: tuple[tuple[str, str, int], ...] = (("source", "10.0.0.1", 9000),),
        target_hosts: tuple[tuple[str, str, int], ...] = (("target", "10.0.0.2", 9000),),
    ) -> None:
        self.database_engine = database_engine
        self.source_hosts = source_hosts
        self.target_hosts = target_hosts
        self.commands: list[str] = []
        self.command_settings: list[object] = []
        self.queries: list[str] = []
        self.closed = False

    def query(self, query: str) -> FakeClickHouseResult:  # noqa: PLR0911
        self.queries.append(query)
        if query == "SHOW CREATE TABLE analytics.events":
            return FakeClickHouseResult([(TABLE_DDL,)])
        if query == "SHOW CREATE TABLE analytics.events_shard":
            return FakeClickHouseResult([(SHARD_DDL,)])
        if query.startswith("SELECT engine FROM system.databases"):
            return FakeClickHouseResult([(self.database_engine,)])
        if "getMacro('cluster')" in query:
            return FakeClickHouseResult([("core",)])
        if "FROM system.clusters" in query and "'core'" in query:
            return FakeClickHouseResult(list(self.source_hosts))
        if "FROM system.clusters" in query and "'archive'" in query:
            return FakeClickHouseResult(list(self.target_hosts))
        if "clusterAllReplicas('archive', system, tables)" in query:
            return FakeClickHouseResult([(0,)])
        if query.startswith("SELECT count() FROM "):
            return FakeClickHouseResult([(3,)])
        if query.startswith("EXISTS TABLE "):
            return FakeClickHouseResult([(1,)])
        return FakeClickHouseResult([])

    def command(self, query: str, settings: object = None) -> None:
        self.commands.append(query)
        self.command_settings.append(settings)

    def close(self) -> None:
        self.closed = True


class CountingReconfigureClient(ReconfigureClient):
    def __init__(self, counts: list[int]) -> None:
        super().__init__()
        self.counts = counts

    def query(self, query: str) -> FakeClickHouseResult:
        if query.startswith("SELECT count() FROM ") and self.counts:
            self.queries.append(query)
            return FakeClickHouseResult([(self.counts.pop(0),)])
        return super().query(query)


class LocalReconfigureClient(ReconfigureClient):
    def query(self, query: str) -> FakeClickHouseResult:
        if query == "SHOW CREATE TABLE analytics.local_events":
            self.queries.append(query)
            return FakeClickHouseResult([(LOCAL_DDL,)])
        return super().query(query)


def _options(**overrides: object) -> ChReconfigureOptions:
    values: dict[str, object] = {
        "connection_key": "ch",
        "table": "analytics.events",
        "ch_engine": None,
        "ch_partition_by": None,
        "ch_order_by": None,
        "ch_cluster": None,
        "ch_source_cluster": None,
        "ch_sharding_key": None,
        "ch_settings": None,
        "ch_reset_partition_by": False,
        "ch_reset_order_by": False,
        "validate_row_count": True,
        "query_label": None,
    }
    values.update(overrides)
    return ChReconfigureOptions(**values)  # type: ignore[arg-type]


def test_settings_change_uses_direct_alter_and_preserves_ddl() -> None:
    client = ReconfigureClient()

    reconfiguration = plan_ch_table_reconfiguration(
        get_backend_adapter("ch"),
        client,
        _options(ch_settings={"index_granularity": 4096, "old_setting": None}),
    )

    assert reconfiguration.strategy == "settings"
    assert reconfiguration.plan.sqls == [
        "ALTER TABLE analytics.events_shard ON CLUSTER '{cluster}' "
        "MODIFY SETTING index_granularity=4096",
        "ALTER TABLE analytics.events_shard ON CLUSTER '{cluster}' RESET SETTING old_setting",
    ]
    assert "INDEX idx_id" in reconfiguration.after_ddl["shard"]
    assert "index_granularity = 4096" in reconfiguration.after_ddl["shard"]


def test_structural_change_builds_atomic_managed_pair_plan() -> None:
    client = ReconfigureClient()

    reconfiguration = plan_ch_table_reconfiguration(
        get_backend_adapter("ch"),
        client,
        _options(
            ch_engine="MergeTree",
            ch_partition_by="toMonday(dt)",
            ch_order_by=["id", "dt"],
        ),
    )

    assert reconfiguration.strategy == "managed_pair_rebuild"
    rendered = "\n".join(reconfiguration.plan.sqls)
    assert "ENGINE=MergeTree" in rendered
    assert any(
        partition_sql in rendered
        for partition_sql in (
            "PARTITION BY toMonday(dt)",
            "PARTITION BY dateTrunc('WEEK', dt)",
        )
    )
    assert 'ORDER BY ("id", "dt")' in rendered
    assert "INSERT INTO analytics.events__reconfigure_" in rendered
    assert "EXCHANGE TABLES analytics.events_shard AND" in rendered
    assert "INDEX idx_id" in rendered


def test_reset_flags_remove_partition_and_restore_tuple_order() -> None:
    reconfiguration = plan_ch_table_reconfiguration(
        get_backend_adapter("ch"),
        ReconfigureClient(),
        _options(ch_reset_partition_by=True, ch_reset_order_by=True),
    )

    desired = reconfiguration.after_ddl["shard"]
    assert "PARTITION BY" not in desired
    assert "ORDER BY tuple()" in desired


def test_already_satisfied_change_returns_no_op_plan() -> None:
    reconfiguration = plan_ch_table_reconfiguration(
        get_backend_adapter("ch"),
        ReconfigureClient(),
        _options(
            ch_engine="ReplicatedMergeTree('/clickhouse/{table}', '{replica}')",
        ),
    )

    assert reconfiguration.strategy == "no_op"
    assert reconfiguration.plan.sqls == []


def test_cross_cluster_plan_routes_wrapper_and_drops_source_shard() -> None:
    reconfiguration = plan_ch_table_reconfiguration(
        get_backend_adapter("ch"),
        ReconfigureClient(),
        _options(ch_cluster="archive", ch_engine="MergeTree"),
    )

    assert reconfiguration.strategy == "cross_cluster_rebuild"
    rendered = "\n".join(reconfiguration.plan.sqls)
    assert "ON CLUSTER 'archive'" in rendered
    assert "Distributed('archive', 'analytics', 'events_shard', rand())" in rendered
    assert "DROP TABLE IF EXISTS analytics.events_shard ON CLUSTER '{cluster}'" in rendered


def test_partially_overlapping_clusters_are_rejected() -> None:
    shared = ("shared", "10.0.0.1", 9000)
    client = ReconfigureClient(
        source_hosts=(shared, ("source", "10.0.0.2", 9000)),
        target_hosts=(shared, ("target", "10.0.0.3", 9000)),
    )

    with pytest.raises(InvalidSqlInputError, match="Partially overlapping"):
        plan_ch_table_reconfiguration(
            get_backend_adapter("ch"),
            client,
            _options(ch_cluster="archive", ch_engine="MergeTree"),
        )


def test_ordinary_database_plan_uses_rename_fallback() -> None:
    reconfiguration = plan_ch_table_reconfiguration(
        get_backend_adapter("ch"),
        ReconfigureClient(database_engine="Ordinary"),
        _options(ch_engine="MergeTree"),
    )

    rendered = "\n".join(reconfiguration.plan.sqls)
    assert "RENAME TABLE analytics.events_shard TO analytics.events_shard__backup_" in rendered
    assert "EXCHANGE TABLES" not in rendered
    assert len(reconfiguration.rollback_sqls) == 2


def test_public_dry_run_and_settings_execution(monkeypatch: pytest.MonkeyPatch) -> None:
    clients: list[ReconfigureClient] = []

    def open_connection(_db_key: str) -> ReconfigureClient:
        client = ReconfigureClient()
        clients.append(client)
        return client

    monkeypatch.setattr(
        reconfigure_api,
        "get_connection_config",
        lambda _db_key: SimpleNamespace(connection_key="ch", backend="ch"),
    )
    monkeypatch.setattr(reconfigure_api, "get_sql_connection", open_connection)

    plan = sql.ch_reconfigure_table(
        "ch",
        "analytics.events",
        ch_settings={"index_granularity": 4096},
        dry_run=True,
    )
    return_sql_plan = sql.ch_reconfigure_table(
        "ch",
        "analytics.events",
        ch_cluster="core",
        ch_source_cluster="{cluster}",
        ch_sharding_key="cityHash64(id)",
        validate_row_count=False,
        retry_cnt=1,
        timeout_increment=0,
        query_label="test=reconfigure",
        return_sql=True,
    )
    result = sql.ch_reconfigure_table(
        "ch",
        "analytics.events",
        ch_settings={"index_granularity": 4096},
        return_metadata=True,
    )

    assert isinstance(plan, SqlPlan)
    assert isinstance(return_sql_plan, SqlPlan)
    assert isinstance(result, SqlOperationResult)
    assert result.data["strategy"] == "settings"
    assert clients[0].commands == []
    assert clients[1].commands == []
    assert clients[2].commands == [
        "ALTER TABLE analytics.events_shard ON CLUSTER '{cluster}' "
        "MODIFY SETTING index_granularity=4096"
    ]
    assert all(client.closed for client in clients)


def test_public_option_conflicts_fail_before_connecting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        reconfigure_api,
        "get_connection_config",
        lambda _db_key: SimpleNamespace(connection_key="ch", backend="ch"),
    )
    monkeypatch.setattr(
        reconfigure_api,
        "get_sql_connection",
        lambda _db_key: pytest.fail("validation should not open a connection"),
    )

    with pytest.raises(InvalidSqlInputError, match="cannot be combined"):
        sql.ch_reconfigure_table(
            "ch",
            "analytics.events",
            ch_partition_by="dt",
            ch_reset_partition_by=True,
        )


def test_rebuild_execution_validates_and_cleans_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = CountingReconfigureClient([3, 3, 3, 3])
    adapter = get_backend_adapter("ch")
    reconfiguration = plan_ch_table_reconfiguration(
        adapter,
        client,
        _options(ch_engine="MergeTree"),
    )
    monkeypatch.setattr(reconfigure_backend, "_wait_for_created_replacement", lambda *_: None)
    monkeypatch.setattr(reconfigure_backend, "_wait_for_cleanup", lambda *_: None)

    reconfigure_backend.execute_ch_table_reconfiguration(
        adapter,
        client,
        reconfiguration,
        validate_row_count=True,
    )

    assert reconfiguration.cleanup_complete is True
    assert reconfiguration.plan.metadata.row_count_validated is True
    assert any(command.startswith("INSERT INTO ") for command in client.commands)
    assert sum(command.startswith("EXCHANGE TABLES ") for command in client.commands) == 1
    assert any(command.startswith("DROP TABLE IF EXISTS ") for command in client.commands)
    assert all(
        settings
        == {
            "distributed_ddl_task_timeout": 300,
            "distributed_ddl_output_mode": "throw_only_active",
        }
        for command, settings in zip(client.commands, client.command_settings)
        if "ON CLUSTER" in command
    )


def test_rebuild_aborts_before_cutover_when_source_count_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = CountingReconfigureClient([3, 3, 4])
    adapter = get_backend_adapter("ch")
    reconfiguration = plan_ch_table_reconfiguration(
        adapter,
        client,
        _options(ch_engine="MergeTree"),
    )
    monkeypatch.setattr(reconfigure_backend, "_wait_for_created_replacement", lambda *_: None)
    monkeypatch.setattr(reconfigure_backend, "_wait_for_cleanup", lambda *_: None)

    with pytest.raises(RuntimeError, match="source row count changed"):
        reconfigure_backend.execute_ch_table_reconfiguration(
            adapter,
            client,
            reconfiguration,
            validate_row_count=True,
        )

    assert not any(command.startswith("EXCHANGE TABLES ") for command in client.commands)


def test_rebuild_rolls_back_failed_post_cutover_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = CountingReconfigureClient([3, 3, 3, 2])
    adapter = get_backend_adapter("ch")
    reconfiguration = plan_ch_table_reconfiguration(
        adapter,
        client,
        _options(ch_engine="MergeTree"),
    )
    monkeypatch.setattr(reconfigure_backend, "_wait_for_created_replacement", lambda *_: None)
    monkeypatch.setattr(reconfigure_backend, "_wait_for_cleanup", lambda *_: None)

    with pytest.raises(RuntimeError, match="during cutover"):
        reconfigure_backend.execute_ch_table_reconfiguration(
            adapter,
            client,
            reconfiguration,
            validate_row_count=True,
        )

    assert sum(command.startswith("EXCHANGE TABLES ") for command in client.commands) == 2


def test_standalone_table_builds_local_rebuild_plan() -> None:
    reconfiguration = plan_ch_table_reconfiguration(
        get_backend_adapter("ch"),
        LocalReconfigureClient(),
        _options(table="analytics.local_events", ch_order_by="id"),
    )

    assert reconfiguration.strategy == "local_rebuild"
    assert reconfiguration.source_cluster is None
    assert "ON CLUSTER" not in "\n".join(reconfiguration.plan.sqls)


@pytest.mark.parametrize(
    ("table_ddl", "message"),
    [
        (
            TABLE_DDL.replace("'events_shard'", "'external_events'"),
            "managed Distributed/_shard pair",
        ),
        (
            TABLE_DDL.replace(
                "Distributed('{cluster}', 'analytics', 'events_shard', rand())",
                "Distributed()",
            ),
            "unsupported Distributed engine",
        ),
    ],
)
def test_managed_pair_shape_is_validated(
    monkeypatch: pytest.MonkeyPatch,
    table_ddl: str,
    message: str,
) -> None:
    monkeypatch.setattr(
        reconfigure_backend,
        "_show_create_table",
        lambda _connection, _table: table_ddl,
    )

    with pytest.raises(InvalidSqlInputError, match=message):
        plan_ch_table_reconfiguration(
            get_backend_adapter("ch"),
            ReconfigureClient(),
            _options(ch_settings={"index_granularity": 4096}),
        )


def test_source_cluster_must_match_distributed_engine() -> None:
    with pytest.raises(InvalidSqlInputError, match="does not match"):
        plan_ch_table_reconfiguration(
            get_backend_adapter("ch"),
            ReconfigureClient(),
            _options(
                ch_source_cluster="archive",
                ch_settings={"index_granularity": 4096},
            ),
        )


def test_cross_cluster_requires_managed_pair() -> None:
    with pytest.raises(InvalidSqlInputError, match="requires a managed"):
        plan_ch_table_reconfiguration(
            get_backend_adapter("ch"),
            LocalReconfigureClient(),
            _options(
                table="analytics.local_events",
                ch_source_cluster="core",
                ch_cluster="archive",
                ch_engine="MergeTree",
            ),
        )


def test_reconfiguration_requires_at_least_one_change() -> None:
    with pytest.raises(InvalidSqlInputError, match="At least one"):
        plan_ch_table_reconfiguration(
            get_backend_adapter("ch"),
            ReconfigureClient(),
            _options(),
        )


def test_cross_cluster_refuses_existing_destination_shard() -> None:
    class ExistingDestinationClient(ReconfigureClient):
        def query(self, query: str) -> FakeClickHouseResult:
            if "clusterAllReplicas('archive', system, tables)" in query:
                self.queries.append(query)
                return FakeClickHouseResult([(1,)])
            return super().query(query)

    with pytest.raises(InvalidSqlInputError, match="already contains"):
        plan_ch_table_reconfiguration(
            get_backend_adapter("ch"),
            ExistingDestinationClient(),
            _options(ch_cluster="archive", ch_engine="MergeTree"),
        )


def test_wrapper_only_change_uses_wrapper_recreate_strategy() -> None:
    reconfiguration = plan_ch_table_reconfiguration(
        get_backend_adapter("ch"),
        ReconfigureClient(),
        _options(ch_sharding_key="cityHash64(id)"),
    )

    assert reconfiguration.strategy == "wrapper_recreate"
    assert reconfiguration.replacement_table is not None


def test_noop_execution_finishes_without_commands() -> None:
    client = ReconfigureClient()
    adapter = get_backend_adapter("ch")
    reconfiguration = plan_ch_table_reconfiguration(
        adapter,
        client,
        _options(ch_engine="ReplicatedMergeTree('/clickhouse/{table}', '{replica}')"),
    )

    reconfigure_backend.execute_ch_table_reconfiguration(
        adapter,
        client,
        reconfiguration,
        validate_row_count=True,
    )

    assert reconfiguration.cleanup_complete is True
    assert client.commands == []


def test_rebuild_detects_replacement_count_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = CountingReconfigureClient([3, 2, 3])
    adapter = get_backend_adapter("ch")
    reconfiguration = plan_ch_table_reconfiguration(
        adapter,
        client,
        _options(ch_engine="MergeTree"),
    )
    monkeypatch.setattr(reconfigure_backend, "_wait_for_created_replacement", lambda *_: None)

    with pytest.raises(RuntimeError, match="replacement row count"):
        reconfigure_backend.execute_ch_table_reconfiguration(
            adapter,
            client,
            reconfiguration,
            validate_row_count=True,
        )


def test_failed_rollback_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    client = CountingReconfigureClient([3, 3, 3])
    adapter = get_backend_adapter("ch")
    reconfiguration = plan_ch_table_reconfiguration(
        adapter,
        client,
        _options(ch_engine="MergeTree"),
    )
    monkeypatch.setattr(reconfigure_backend, "_wait_for_created_replacement", lambda *_: None)

    def execute_phase(_adapter: object, _connection: object, _plan: object, phase: str) -> None:
        if phase == "cutover":
            message = "cutover failed"
            raise RuntimeError(message)

    monkeypatch.setattr(reconfigure_backend, "_execute_phase", execute_phase)
    monkeypatch.setattr(
        reconfigure_backend,
        "_execute_sqls",
        lambda *_: (_ for _ in ()).throw(RuntimeError("rollback failed")),
    )

    with pytest.raises(RuntimeError, match="rollback also failed"):
        reconfigure_backend.execute_ch_table_reconfiguration(
            adapter,
            client,
            reconfiguration,
            validate_row_count=True,
        )


def test_cleanup_failure_is_returned_as_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = CountingReconfigureClient([3, 3, 3, 3])
    adapter = get_backend_adapter("ch")
    reconfiguration = plan_ch_table_reconfiguration(
        adapter,
        client,
        _options(ch_engine="MergeTree"),
    )
    monkeypatch.setattr(reconfigure_backend, "_wait_for_created_replacement", lambda *_: None)
    monkeypatch.setattr(
        reconfigure_backend,
        "_wait_for_cleanup",
        lambda *_: (_ for _ in ()).throw(TimeoutError("still visible")),
    )

    reconfigure_backend.execute_ch_table_reconfiguration(
        adapter,
        client,
        reconfiguration,
        validate_row_count=False,
    )

    assert reconfiguration.cleanup_complete is False
    assert reconfiguration.cleanup_error == "TimeoutError: still visible"


def test_wait_and_best_effort_cleanup_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        reconfigure_backend,
        "_wait_for_ch_table",
        lambda _connection, table: calls.append(("local", table)),
    )
    monkeypatch.setattr(
        reconfigure_backend,
        "_wait_for_ch_table_on_cluster",
        lambda _connection, table, *, ch_cluster: calls.append((ch_cluster, table)),
    )
    monkeypatch.setattr(
        reconfigure_backend,
        "_wait_for_ch_table_absence",
        lambda _connection, table: calls.append(("absent", table)),
    )
    monkeypatch.setattr(
        reconfigure_backend,
        "_wait_for_ch_table_absence_on_cluster",
        lambda _connection, table, *, ch_cluster: calls.append((f"absent:{ch_cluster}", table)),
    )
    reconfiguration = SimpleNamespace(
        replacement_table="analytics.wrapper_tmp",
        strategy="cross_cluster_rebuild",
        temporary_tables=["analytics.wrapper_tmp", "analytics.events_shard"],
        target_cluster="archive",
        source_cluster="core",
        cleanup_tables=[("analytics.local_tmp", None), ("analytics.cluster_tmp", "core")],
    )

    reconfigure_backend._wait_for_created_replacement(None, reconfiguration)
    reconfigure_backend._wait_for_cleanup(None, reconfiguration)
    reconfiguration.strategy = "local_rebuild"
    reconfigure_backend._wait_for_created_replacement(None, reconfiguration)
    reconfiguration.strategy = "cross_cluster_rebuild"
    reconfiguration.target_cluster = None
    reconfigure_backend._wait_for_created_replacement(None, reconfiguration)
    reconfiguration.replacement_table = None
    reconfigure_backend._wait_for_created_replacement(None, reconfiguration)

    class CleanupAdapter:
        def __init__(self) -> None:
            self.commands: list[str] = []

        def execute_command(self, _connection: object, sql_text: str) -> None:
            self.commands.append(sql_text)
            if len(self.commands) == 1:
                message = "best effort"
                raise RuntimeError(message)

    cleanup_adapter = CleanupAdapter()
    reconfigure_backend._best_effort_cleanup(cleanup_adapter, None, reconfiguration)

    assert ("archive", "analytics.events_shard") in calls
    assert ("absent", "analytics.local_tmp") in calls
    assert ("absent:core", "analytics.cluster_tmp") in calls
    assert len(cleanup_adapter.commands) == 2


def test_backend_metadata_helpers_cover_empty_and_unqualified_cases() -> None:
    empty = SimpleNamespace(query=lambda _sql: FakeClickHouseResult([]))
    assert reconfigure_backend._count_rows(empty, "analytics.events") == 0
    assert reconfigure_backend._resolve_optional_cluster(empty, None) is None
    assert reconfigure_backend._table_exists_on_cluster(empty, "analytics.events", None) is False
    with pytest.raises(InvalidSqlInputError, match="does not exist"):
        reconfigure_backend._show_create_table(empty, "analytics.missing")
    with pytest.raises(InvalidSqlInputError, match="returned no rows"):
        reconfigure_backend._query_scalar(empty, "SELECT nothing")
    with pytest.raises(InvalidSqlInputError, match="must not be empty"):
        reconfigure_backend._non_empty_string(" ", "value")

    create = reconfigure_ddl.parse_create_table(
        "CREATE TABLE local_events (id UInt8) ENGINE=MergeTree ORDER BY id",
        "local_events",
    )
    current = SimpleNamespace(query=lambda _sql: FakeClickHouseResult([("analytics",)]))
    assert reconfigure_backend._table_database(current, create) == "analytics"
    assert reconfigure_backend._qualify_like("local_events", "shard") == "shard"
    assert reconfigure_backend._qualify_with_database("local_events", "analytics") == (
        "analytics.local_events"
    )
    assert reconfigure_backend._qualify_with_database("analytics.local_events", "other") == (
        "analytics.local_events"
    )


def test_cluster_comparison_validates_empty_and_equal_hosts() -> None:
    client = ReconfigureClient(source_hosts=(), target_hosts=())
    with pytest.raises(InvalidSqlInputError, match="source cluster"):
        reconfigure_backend._is_cross_cluster(client, "core", "archive")
    client = ReconfigureClient(target_hosts=())
    with pytest.raises(InvalidSqlInputError, match="target cluster"):
        reconfigure_backend._is_cross_cluster(client, "core", "archive")
    same = (("same", "10.0.0.1", 9000),)
    client = ReconfigureClient(source_hosts=same, target_hosts=same)
    assert reconfigure_backend._is_cross_cluster(client, "core", "archive") is False
    assert reconfigure_backend._is_cross_cluster(client, "core", "core") is False


def test_reconfigure_ddl_validation_and_value_rendering() -> None:
    with pytest.raises(InvalidSqlInputError, match="Could not parse"):
        reconfigure_ddl.parse_create_table("CREATE TABLE", "broken")
    with pytest.raises(InvalidSqlInputError, match="not a supported"):
        reconfigure_ddl.parse_create_table("SELECT 1", "query")
    with pytest.raises(InvalidSqlInputError, match="no explicit schema"):
        reconfigure_ddl.parse_create_table("CREATE TABLE x AS SELECT 1", "x")

    no_properties = exp.Create(this=exp.Schema(this=exp.to_table("x")), kind="TABLE")
    with pytest.raises(InvalidSqlInputError, match="does not define an engine"):
        reconfigure_ddl.engine_sql(no_properties)
    with pytest.raises(InvalidSqlInputError, match="does not define an engine"):
        reconfigure_ddl.engine_name(no_properties)
    with pytest.raises(InvalidSqlInputError, match="MergeTree-family"):
        reconfigure_ddl.require_merge_tree(
            reconfigure_ddl.parse_create_table(
                "CREATE TABLE x (id UInt8) ENGINE=Log",
                "x",
            ),
            "x",
        )
    with pytest.raises(InvalidSqlInputError, match="no column schema"):
        reconfigure_ddl.retarget_create(
            exp.Create(this=exp.to_table("x"), kind="TABLE"),
            "y",
            None,
        )

    with pytest.raises(InvalidSqlInputError, match="Invalid expression"):
        reconfigure_ddl.expression_sql("(", "expression")
    for invalid in ({"id": 1}, b"id"):
        with pytest.raises(InvalidSqlInputError, match="SQL expression"):
            reconfigure_ddl.expression_sql(invalid, "expression")
    with pytest.raises(InvalidSqlInputError, match="must not be empty"):
        reconfigure_ddl.expression_sql([], "expression")
    with pytest.raises(InvalidSqlInputError, match="duplicates"):
        reconfigure_ddl.expression_sql(["id", "id"], "expression")
    assert reconfigure_ddl.expression_sql(["id"], "expression") == "`id`"

    assert reconfigure_ddl.setting_value_sql(True) == "1"
    assert reconfigure_ddl.setting_value_sql(3) == "3"
    assert reconfigure_ddl.setting_value_sql(1.5) == "1.5"
    assert reconfigure_ddl.setting_value_sql("value") == "'value'"
    with pytest.raises(InvalidSqlInputError, match="finite"):
        reconfigure_ddl.setting_value_sql(float("inf"))
    with pytest.raises(InvalidSqlInputError, match="strings"):
        reconfigure_ddl.setting_value_sql(object())
    with pytest.raises(InvalidSqlInputError, match="setting name"):
        reconfigure_ddl.normalize_setting_name("bad-name")
    with pytest.raises(InvalidSqlInputError, match="must not be empty"):
        reconfigure_ddl._non_empty_string("", "value")


def test_reconfigure_ddl_replicated_path_and_property_edges() -> None:
    create = reconfigure_ddl.parse_create_table(
        "CREATE TABLE x (id UInt8) ENGINE=MergeTree ORDER BY id",
        "x",
    )
    transformed = reconfigure_ddl.retarget_create(create, "y", "core")
    assert "ON CLUSTER 'core'" in transformed.sql(dialect="clickhouse")
    with pytest.raises(InvalidSqlInputError, match="must contain"):
        reconfigure_ddl.transform_create_table(
            create,
            table_name="x",
            execution_cluster=None,
            ch_engine="ReplicatedMergeTree('/fixed/path', '{replica}')",
            ch_partition_by=None,
            ch_order_by=None,
            ch_settings=None,
            ch_reset_partition_by=False,
            ch_reset_order_by=False,
        )
    reconfigure_ddl._validate_replicated_path(create)

    distributed = reconfigure_ddl.parse_create_table(
        "CREATE TABLE x (id UInt8) ENGINE=Distributed('core', 'db', 'shard')",
        "x",
    )
    assert reconfigure_ddl._distributed_sharding_key(distributed) is None
    assert reconfigure_ddl._distributed_sharding_key(create) is None
    city_hash = reconfigure_ddl.parse_create_table(
        "CREATE TABLE x (id UInt8) ENGINE=Distributed('core', 'db', 'shard', cityHash64(id))",
        "x",
    )
    assert reconfigure_ddl._distributed_sharding_key(city_hash) == "cityHash64(id)"
    assert reconfigure_ddl.distributed_table_parts("events") == ("default", "events")

    settings_free = reconfigure_ddl.parse_create_table(
        "CREATE TABLE x (id UInt8) ENGINE=MergeTree ORDER BY id",
        "x",
    )
    reconfigure_ddl._apply_settings_to_create(settings_free, {"index_granularity": 4096})
    assert "index_granularity = 4096" in settings_free.sql(dialect="clickhouse")
    unusual_settings = exp.Create(
        this=exp.Schema(this=exp.to_table("x")),
        kind="TABLE",
        properties=exp.Properties(
            expressions=[exp.SettingsProperty(expressions=[exp.Literal.number(1)])]
        ),
    )
    reconfigure_ddl._apply_settings_to_create(unusual_settings, {"new_setting": 1})

    replicated_without_args = exp.Create(
        this=exp.Schema(this=exp.to_table("x")),
        kind="TABLE",
        properties=exp.Properties(
            expressions=[exp.EngineProperty(this=exp.Anonymous(this="ReplicatedMergeTree"))]
        ),
    )
    reconfigure_ddl._validate_replicated_path(replicated_without_args)
    replicated_dynamic_path = replicated_without_args.copy()
    engine_property = reconfigure_ddl._property(
        replicated_dynamic_path,
        exp.EngineProperty,
    )
    assert isinstance(engine_property, exp.EngineProperty)
    assert isinstance(engine_property.this, exp.Anonymous)
    engine_property.this.append("expressions", exp.column("path_column"))
    reconfigure_ddl._validate_replicated_path(replicated_dynamic_path)

    transient_type = type("UuidProperty", (exp.Expression,), {})
    assert reconfigure_ddl._is_transient_create_property(transient_type()) is True


def test_synchronous_execution_falls_back_for_simple_clients() -> None:
    class LegacyClient(ReconfigureClient):
        def command(self, query: str, settings: object = None) -> None:
            if settings is not None:
                message = "settings unsupported"
                raise TypeError(message)
            super().command(query)

    client = LegacyClient()
    reconfigure_execution.execute_reconfiguration_sqls(
        get_backend_adapter("ch"),
        client,
        [
            "ALTER TABLE analytics.events ON CLUSTER core MODIFY COMMENT 'x'",
            "OPTIMIZE TABLE x",
        ],
    )

    assert len(client.commands) == 2
    assert reconfigure_execution.cluster_clause(None) == ""


@pytest.mark.parametrize(
    "kwargs",
    [
        {"ch_order_by": "id", "ch_reset_order_by": True},
        {"ch_settings": ["not", "a", "mapping"]},
        {"validate_row_count": "yes"},
    ],
)
def test_public_validation_rejects_invalid_option_types(
    monkeypatch: pytest.MonkeyPatch,
    kwargs: dict[str, object],
) -> None:
    monkeypatch.setattr(
        reconfigure_api,
        "get_connection_config",
        lambda _db_key: SimpleNamespace(connection_key="ch", backend="ch"),
    )
    with pytest.raises(InvalidSqlInputError):
        sql.ch_reconfigure_table("ch", "analytics.events", **kwargs)


def test_public_rejects_non_clickhouse_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        reconfigure_api,
        "get_connection_config",
        lambda _db_key: SimpleNamespace(connection_key="trino", backend="trino"),
    )
    with pytest.raises(UnsupportedConnectionTypeError):
        sql.ch_reconfigure_table(
            "trino",
            "analytics.events",
            ch_settings={"index_granularity": 4096},
        )


def test_reset_only_setting_sql_and_public_none_return(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert reconfigure_backend._build_setting_alter_sqls(
        "analytics.events",
        {"old_setting": None},
        ch_cluster=None,
    ) == ["ALTER TABLE analytics.events RESET SETTING old_setting"]
    monkeypatch.setattr(
        reconfigure_api,
        "get_connection_config",
        lambda _db_key: SimpleNamespace(connection_key="ch", backend="ch"),
    )
    monkeypatch.setattr(reconfigure_api, "get_sql_connection", lambda _db_key: ReconfigureClient())

    result = sql.ch_reconfigure_table(
        "ch",
        "analytics.events",
        ch_settings={"index_granularity": 4096},
        retry_cnt=1,
    )

    assert result is None


def test_public_failure_builds_operation_context(monkeypatch: pytest.MonkeyPatch) -> None:
    class FailingClient(ReconfigureClient):
        def query(self, query: str) -> FakeClickHouseResult:
            message = f"failed query: {query}"
            raise RuntimeError(message)

    monkeypatch.setattr(
        reconfigure_api,
        "get_connection_config",
        lambda _db_key: SimpleNamespace(connection_key="ch", backend="ch"),
    )
    monkeypatch.setattr(reconfigure_api, "get_sql_connection", lambda _db_key: FailingClient())

    with pytest.raises(RuntimeError, match="failed query"):
        sql.ch_reconfigure_table(
            "ch",
            "analytics.events",
            ch_settings={"index_granularity": 4096},
            retry_cnt=1,
        )
