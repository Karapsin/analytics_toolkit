from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pandas as pd
import pytest
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.ch.creation_policy import ClickHouseCreationPolicy
from analytics_toolkit.sql.backends.ch.routing import ChClusterRouting, wrap_client
from analytics_toolkit.sql.backends.ch.safe_replace import (
    _create_replacement_facade,
    _database_engine_on_scope,
    _validate_nonreplicated_facade_cluster,
)
from analytics_toolkit.sql.backends.models import StageFinalizationRequest
from analytics_toolkit.sql.connection.errors import (
    AmbiguousSqlReplaceError,
    InvalidSqlInputError,
    SqlConfigError,
)
from sqlglot import exp

from tests.sql._support.ch_replace import (
    LOCAL,
    PHYSICAL,
    STAGE,
    STAGED_ROWS,
    TARGET,
    ReplacementClient,
    Table,
)


def _request(connection, *, shard_scope="core", facade_scope=None, only_shard=False):
    return StageFinalizationRequest(
        connection=connection,
        connection_key="ch",
        stage_table=STAGE,
        target_table=PHYSICAL if only_shard else TARGET,
        replace_target_table=True,
        target_exists=True,
        sample_batch=pd.DataFrame({"id": [1], "value": ["new"]}),
        target_column_types={"id": "Int64", "value": "String"},
        insert_column_types={"id": "Int64", "value": "String"},
        ch_sharding_key="cityHash64(id)",
        ch_only_shard=only_shard,
        query_label="replacement topology",
        ch_creation_policy=ClickHouseCreationPolicy(
            create_distributed_pair=not only_shard,
            shard_engine="ReplicatedMergeTree",
            shard_on_cluster=shard_scope,
            distributed_engine_template=(
                "Distributed({cluster}, {database}, {shard_table}, {sharding_key})"
            ),
            distributed_cluster=shard_scope or connection.storage,
            distributed_on_cluster=facade_scope,
            sharding_key="cityHash64(id)",
            ddl_ready_timeout_seconds=1,
            ddl_wait_policy="wait_none",
        ),
    )


def test_guarded_replacement_preserves_configured_distributed_template_arguments() -> None:
    client = ReplacementClient()
    request = _request(client)
    request = replace(
        request,
        ch_creation_policy=replace(
            request.ch_creation_policy,
            distributed_engine_template=(
                "Distributed({cluster}, {database}, {shard_table}, {sharding_key}, 'async_policy')"
            ),
        ),
    )
    _create_replacement_facade(
        get_backend_adapter("ch"),
        request,
        replacement_facade="sandbox.new_facade",
        physical_target=PHYSICAL,
        facade_cluster="core",
        distributed_cluster="core",
    )
    statement, _ = client.commands[-1]
    assert "    'async_policy'" in statement
    assert "    cityHash64(id)," in statement


def test_local_policy_controls_guarded_replacement_without_shard_alias() -> None:
    client = ReplacementClient(coordinator_scope="core")
    client.replica_count = 1
    request = replace(_request(client, only_shard=True), ch_only_shard=False)
    get_backend_adapter("ch").finalize_stage_table(request)
    assert client.tables["core", PHYSICAL].rows == STAGED_ROWS
    assert all(f"{PHYSICAL}_shard" not in sql for sql, _settings in client.commands)


def test_legacy_finalization_preserves_unrelated_companion_before_conversion() -> None:
    class LocalConversionClient(ReplacementClient):
        identity = "00000000-0000-0000-0000-000000000001"

        def __init__(self):
            super().__init__(storage=LOCAL)
            self.replica_count = 1

        def query(self, statement, **kwargs):
            if statement == "SELECT serverUUID(), tcpPort()":
                self.queries.append(statement)
                return SimpleNamespace(result_rows=[(self.identity, 9000)])
            return super().query(statement, **kwargs)

        def _metadata_rows(self, tree, scope, table):
            fields = tuple(item.name for item in tree.expressions)
            if table == "system.clusters" and "is_local" in fields:
                assert scope == LOCAL
                assert fields == (
                    "shard_num",
                    "replica_num",
                    "host_name",
                    "host_address",
                    "port",
                    "is_local",
                )
                return [(1, 1, "coordinator", "127.0.0.1", 9000, 1)]
            if table == "system.one" and "_shard_num" in fields:
                assert scope == LOCAL
                return [(1, self.identity, 9000)]
            if table == "system.tables" and fields == ("_shard_num", "create_table_query"):
                assert scope == LOCAL
                filters = {item.this.name: item.expression.this for item in tree.find_all(exp.EQ)}
                name = f"{filters['database']}.{filters['name']}"
                state = self.tables.get((LOCAL, name))
                if state is None:
                    return []
                columns = ", ".join(f"`{column}` Int64" for column in state.columns)
                return [
                    (
                        1,
                        f"CREATE TABLE {name} ({columns}) "
                        "ENGINE = ReplicatedMergeTree ORDER BY tuple()",
                    )
                ]
            return super()._metadata_rows(tree, scope, table)

    client = LocalConversionClient()
    client.facade_sources.pop((LOCAL, TARGET))
    client.tables[LOCAL, TARGET] = Table(17, ("old_column",))
    client.tables[LOCAL, PHYSICAL] = Table(100, ("unrelated_column",))
    original = dict(client.tables)
    original_ddl = {
        name: client.query(f"SHOW CREATE TABLE {name}").result_rows[0][0]
        for name in (TARGET, PHYSICAL)
    }
    request = replace(_request(client), ch_creation_policy=None)
    with pytest.raises(InvalidSqlInputError, match="already contains"):
        get_backend_adapter("ch").finalize_stage_table(request)
    assert client.tables[LOCAL, PHYSICAL].rows == 100
    assert client.tables[LOCAL, TARGET].rows == 17
    assert client.tables == original
    assert all(client.tables[identity] is table for identity, table in original.items())
    assert {
        name: client.query(f"SHOW CREATE TABLE {name}").result_rows[0][0]
        for name in (TARGET, PHYSICAL)
    } == original_ddl
    assert client.commands == []


@pytest.mark.parametrize("native", [False, True], ids=["http", "native"])
@pytest.mark.parametrize("only_shard", [False, True], ids=["pair", "shard"])
@pytest.mark.parametrize("engine", ["ReplicatedMergeTree", "MergeTree"])
def test_replace_loads_remote_storage_from_local_stage(native, only_shard, engine) -> None:
    client = ReplacementClient(coordinator_scope="core" if only_shard else LOCAL)
    if only_shard:
        client.replica_count = 1
        client.validation_replica = 0
    client.is_native_transport = native
    original_local = Table(23, ("unrelated_local_column",))
    client.tables[LOCAL, PHYSICAL] = original_local

    request = _request(client, only_shard=only_shard)
    request = replace(
        request, ch_creation_policy=replace(request.ch_creation_policy, shard_engine=engine)
    )
    get_backend_adapter("ch").finalize_stage_table(request)

    state = client.tables["core", PHYSICAL]
    assert state.rows == STAGED_ROWS
    assert state.columns == ("id", "value")
    assert state.replicated == (engine == "ReplicatedMergeTree")
    assert client.tables[LOCAL, PHYSICAL] == original_local
    assert client.loads[0][0][0] == "core"
    assert client.loads[0][1] == ("core" if only_shard else LOCAL, STAGE)
    assert bool(client.local_loads) is only_shard
    assert {scope for scope, _left, _right in client.swaps} == (
        {"core"} if only_shard else {"core", LOCAL}
    )
    assert len(client.swaps) == (1 if only_shard else 2)
    assert len(client.cleanup_markers) == len(client.swaps)
    assert all(
        scope == ("core" if left.startswith(PHYSICAL) else LOCAL)
        and right == left + "__discardable"
        for scope, left, right in client.cleanup_markers
    )
    assert not any("__replace_" in table or "__backup_" in table for _scope, table in client.tables)
    insert, settings = next(
        (sql, settings) for sql, settings in client.commands if "INSERT INTO" in sql
    )
    data_function = "clusterAllReplicas" if only_shard and engine == "MergeTree" else "cluster"
    assert f"FUNCTION {data_function}('core', 'sandbox', 'events_shard__replace_" in insert
    assert "cityHash64(id)" in insert
    assert "(`id`, `value`) SELECT CAST(`id` AS Int64)" in insert
    assert "query_label=replacement topology" in insert
    assert settings["distributed_foreground_insert"] == 1
    counts = [sql for sql in client.queries if sql.startswith("/* analytics_toolkit")]
    assert len(counts) == 2
    assert all(f"FROM {data_function}('core', 'sandbox', 'events_shard" in sql for sql in counts)
    assert not any(
        "DROP TABLE" in sql and "ON CLUSTER" not in sql and PHYSICAL in sql
        for sql, _ in client.commands
    )
    if not only_shard:
        for replica in (0, 1):
            client.validation_replica = replica
            assert client.query(
                "SELECT count() FROM cluster('core', 'sandbox', 'events')"
            ).result_rows == [(STAGED_ROWS,)]


def test_replace_preserves_stage_replica_routing_without_changing_storage_scope() -> None:
    client = ReplacementClient(stage_scope="ingest")
    connection = wrap_client(
        client,
        SimpleNamespace(cluster_routing=ChClusterRouting("ingest"), database="sandbox"),
    )

    get_backend_adapter("ch").finalize_stage_table(_request(connection))

    assert client.tables["core", PHYSICAL].rows == STAGED_ROWS
    assert client.loads[0][1] == ("ingest", STAGE)
    insert, settings = next(
        (sql, options) for sql, options in client.commands if "INSERT INTO" in sql
    )
    assert "INSERT INTO FUNCTION cluster('core'," in insert
    assert "(`id`, `value`) SELECT" in insert
    assert settings["distributed_foreground_insert"] == 1
    assert "FROM clusterAllReplicas('ingest', 'stage'," in insert
    assert {scope for scope, _left, _right in client.swaps} == {"core", LOCAL}
    assert not any("ON CLUSTER 'ingest'" in sql for sql, _ in client.commands)


def test_cleanup_marker_failure_preserves_old_rows_when_entry_host_is_a_cluster_member() -> None:
    class FailedMarkerClient(ReplacementClient):
        def command(self, sql, settings=None) -> None:
            if sql.startswith("RENAME TABLE") and "__discardable" in sql:
                self.commands.append((sql, settings or {}))
                message = "cleanup marker unavailable"
                raise ConnectionError(message)
            super().command(sql, settings=settings)

    client = FailedMarkerClient(storage=LOCAL)
    client.engines[LOCAL] = "Atomic"
    get_backend_adapter("ch").finalize_stage_table(
        _request(client, shard_scope=LOCAL, facade_scope=LOCAL)
    )
    assert client.tables[LOCAL, PHYSICAL].rows == STAGED_ROWS
    backups = [
        state
        for (scope, name), state in client.tables.items()
        if scope == LOCAL and name.startswith(PHYSICAL + "__replace_guarded_")
    ]
    assert len(backups) == 1
    assert backups[0].rows == 17
    assert backups[0].columns == ("old_column",)
    assert not any(
        "DROP TABLE" in sql and "__replace_guarded_" in sql for sql, _ in client.commands
    )


@pytest.mark.parametrize("only_shard", [False, True], ids=["pair", "shard"])
def test_replace_legacy_cluster_creates_remote_physical_before_loading(only_shard) -> None:
    client = ReplacementClient(coordinator_scope="core")
    client.replica_count = 1
    client.validation_replica = 0
    request = replace(
        _request(client, only_shard=only_shard),
        ch_creation_policy=None,
        ch_cluster="core",
    )

    get_backend_adapter("ch").finalize_stage_table(request)

    assert client.tables["core", PHYSICAL] == Table(STAGED_ROWS, ("id", "value"))
    assert client.loads[0][0][0] == "core"
    assert all(scope == "core" for scope, _left, _right in client.swaps)
    assert any(
        "CREATE TABLE sandbox.events_shard__replace_" in sql and "ON CLUSTER" in sql
        for sql, _settings in client.commands
    )
    assert not any("__replace_" in table for _scope, table in client.tables)


@pytest.mark.parametrize("bad_final", [False, True], ids=["before-cutover", "after-cutover"])
def test_remote_replace_row_count_failure_preserves_original(bad_final) -> None:
    client = ReplacementClient()
    client.lag_replica = True
    original = client.tables["core", PHYSICAL]
    client.incomplete_load = not bad_final
    client.bad_final_count = bad_final

    with pytest.raises(RuntimeError, match="row count"):
        get_backend_adapter("ch").finalize_stage_table(_request(client))

    assert client.tables["core", PHYSICAL] == original
    assert client.tables[LOCAL, TARGET].columns == ("old_column",)
    assert len(client.swaps) == (4 if bad_final else 0)
    if bad_final:
        assert client.swaps[2:] == list(reversed(client.swaps[:2]))
    assert {scope for scope, _left, _right in client.swaps} == (
        {"core", LOCAL} if bad_final else set()
    )
    assert not any("__replace_" in table for _scope, table in client.tables)


def test_remote_replace_failed_rollback_preserves_recovery_artifacts() -> None:
    client = ReplacementClient()
    client.bad_final_count = True
    client.fail_rollback = True

    with pytest.raises(AmbiguousSqlReplaceError, match="automatic rollback"):
        get_backend_adapter("ch").finalize_stage_table(_request(client))

    assert any(scope == "core" and "__replace_" in table for scope, table in client.tables)
    assert not any(
        scope == LOCAL and table.startswith(PHYSICAL + "__replace_")
        for scope, table in client.tables
    )
    assert any(
        scope == LOCAL and table.startswith(TARGET + "__replace_") for scope, table in client.tables
    )
    assert not any("DROP TABLE" in sql for sql, _ in client.commands)


def test_replacement_after_failed_rollback_retains_original_recovery_data() -> None:
    client = ReplacementClient()
    original = client.tables["core", PHYSICAL]
    client.bad_final_count = True
    client.fail_rollback = True
    request = _request(client)
    with pytest.raises(AmbiguousSqlReplaceError, match="automatic rollback"):
        get_backend_adapter("ch").finalize_stage_table(request)
    recovery = {
        key: state for key, state in client.tables.items() if key[0] == "core" and state == original
    }
    assert recovery
    client.bad_final_count = False
    client.fail_rollback = False
    client.rollback_pending = False
    command_count = len(client.commands)

    with pytest.raises(AmbiguousSqlReplaceError, match="unresolved artifact"):
        get_backend_adapter("ch").finalize_stage_table(request)

    assert all(client.tables[key] == state for key, state in recovery.items())
    assert len(client.commands) == command_count
    assert len(client.loads) == 1


def test_remote_replace_synchronizes_lagging_validation_replica_before_cutover() -> None:
    client = ReplacementClient()
    client.lag_replica = True

    get_backend_adapter("ch").finalize_stage_table(_request(client))

    assert client.syncs[0][0] == "core"
    assert client.syncs[0][2] == (STAGED_ROWS, 0)
    final = client.tables["core", PHYSICAL]
    assert final.replica_rows == (STAGED_ROWS, STAGED_ROWS)
    assert final.replica_rows[client.validation_replica] == STAGED_ROWS
    assert len(client.swaps) == 2
    assert not any("__replace_" in table for _scope, table in client.tables)


def test_remote_replace_sync_failure_leaves_original_untouched() -> None:
    client = ReplacementClient()
    client.lag_replica = True
    client.fail_sync = True
    original = client.tables["core", PHYSICAL]

    with pytest.raises(TimeoutError, match="replica synchronization timed out"):
        get_backend_adapter("ch").finalize_stage_table(_request(client))

    assert client.tables["core", PHYSICAL] == original
    assert client.swaps == []
    assert not any("__replace_" in table for _scope, table in client.tables)


@pytest.mark.parametrize(
    ("database_engine", "boundary"),
    [("Atomic", 1), ("Atomic", 2), *(("Ordinary", index) for index in range(1, 5))],
)
@pytest.mark.parametrize("after_apply", [False, True], ids=["before-execution", "lost-ack"])
def test_partial_cutover_reverses_only_acknowledged_steps_and_preserves_artifacts(
    database_engine, boundary, after_apply
) -> None:
    client = ReplacementClient()
    client.engines["core"] = database_engine
    client.engines[LOCAL] = database_engine
    original = client.tables["core", PHYSICAL]
    original_facade = client.tables[LOCAL, TARGET]
    client.fail_cutover_at = boundary
    client.fail_cutover_after_apply = after_apply

    with pytest.raises(AmbiguousSqlReplaceError, match="preserved for recovery"):
        get_backend_adapter("ch").finalize_stage_table(_request(client))

    assert any(state == original for (scope, _), state in client.tables.items() if scope == "core")
    assert any(
        state == original_facade for (scope, _), state in client.tables.items() if scope == LOCAL
    )
    # Submission without acknowledgement is uncertain even when this fake
    # injected the failure before applying it. Never replay structural inverses.
    assert len(client.swaps) <= boundary
    assert not any("DROP TABLE" in sql for sql, _ in client.commands)


@pytest.mark.parametrize("routed", [False, True])
def test_nonreplicated_facade_rejects_unsafe_replica_configuration_before_cutover(routed) -> None:
    client = ReplacementClient()
    client.internal_replication = not routed
    connection = (
        wrap_client(
            client, SimpleNamespace(cluster_routing=ChClusterRouting("core"), database="sandbox")
        )
        if routed
        else client
    )
    request = _request(connection)
    request = replace(
        request, ch_creation_policy=replace(request.ch_creation_policy, shard_engine="MergeTree")
    )

    with pytest.raises(SqlConfigError, match="replica"):
        get_backend_adapter("ch").finalize_stage_table(request)

    assert client.tables["core", PHYSICAL].rows == 17
    assert client.loads == []
    assert client.commands == []


def test_replace_uses_distributed_data_cluster_separately_from_ddl_scope() -> None:
    client = ReplacementClient()
    client.scope_aliases["deployment"] = "core"
    request = _request(client, shard_scope="deployment")
    request = replace(
        request, ch_creation_policy=replace(request.ch_creation_policy, distributed_cluster="core")
    )

    get_backend_adapter("ch").finalize_stage_table(request)

    insert = next(sql for sql, _ in client.commands if "INSERT INTO" in sql)
    assert "FUNCTION cluster('core'," in insert
    assert any("ON CLUSTER deployment" in sql for sql, _ in client.commands)
    assert client.query("SELECT count() FROM cluster('core', 'sandbox', 'events')").result_rows == [
        (STAGED_ROWS,)
    ]


@pytest.mark.parametrize("only_shard", [False, True], ids=["pair", "shard"])
def test_unqualified_replace_uses_current_database_for_metadata_and_operations(only_shard) -> None:
    client = ReplacementClient(coordinator_scope="core" if only_shard else LOCAL)
    client.engines["core"] = "Ordinary"
    client.engines[LOCAL] = "Ordinary"
    client.database_engines["core", "default"] = "Atomic"
    target = PHYSICAL if only_shard else TARGET
    request = replace(_request(client, only_shard=only_shard), target_table=target.split(".")[1])

    get_backend_adapter("ch").finalize_stage_table(request)

    assert client.tables["core", PHYSICAL].rows == STAGED_ROWS
    assert client.queries.count("SELECT currentDatabase()") == 1
    assert all(
        left.startswith("sandbox.") and right.startswith("sandbox.")
        for _, left, right in client.swaps
    )
    assert not any(sql.startswith("EXCHANGE TABLES") for sql, _ in client.commands)
    metadata = [sql for sql in client.queries if "system, databases" in sql]
    assert metadata
    assert all("name = 'sandbox'" in sql for sql in metadata)
    assert not any("__replace_" in table or "__backup_" in table for _, table in client.tables)


def test_unqualified_replace_detects_writers_in_current_database() -> None:
    client = ReplacementClient()
    client.concurrent_destination_change = True
    request = replace(_request(client), target_table="events")

    with pytest.raises(RuntimeError, match="destination changed"):
        get_backend_adapter("ch").finalize_stage_table(request)

    assert client.swaps == []
    assert client.tables["core", PHYSICAL].rows == 18
    fingerprints = [sql for sql in client.queries if "system, parts" in sql]
    assert len(fingerprints) == 2
    assert all("database = 'sandbox'" in sql for sql in fingerprints)
    assert not any("__replace_" in table for _, table in client.tables)


@pytest.mark.parametrize("bad_final", [False, True])
def test_remote_replace_keeps_separate_facade_scope_and_engine(bad_final) -> None:
    client = ReplacementClient(coordinator_scope="facades")
    client.engines["facades"] = "Ordinary"
    client.bad_final_count = bad_final
    original = client.tables["core", PHYSICAL]
    request = _request(client, facade_scope="facades")

    if bad_final:
        with pytest.raises(RuntimeError, match="final target row count"):
            get_backend_adapter("ch").finalize_stage_table(request)
        assert client.tables["core", PHYSICAL] == original
    else:
        get_backend_adapter("ch").finalize_stage_table(request)
        assert client.tables["core", PHYSICAL].rows == STAGED_ROWS
    assert any(
        sql.startswith("EXCHANGE TABLES") and "ON CLUSTER core" in sql for sql, _ in client.commands
    )
    assert any(
        sql.startswith("RENAME TABLE") and "ON CLUSTER facades" in sql for sql, _ in client.commands
    )
    assert not any("__replace_" in table or "__backup_" in table for _scope, table in client.tables)


@pytest.mark.parametrize("engine", ["ReplicatedMergeTree", "MergeTree"])
@pytest.mark.parametrize("only_shard", [False, True], ids=["pair", "shard"])
def test_local_replace_retains_local_loading_counting_and_rename(engine, only_shard) -> None:
    client = ReplacementClient(storage=LOCAL)
    client.replica_count = 1
    client.validation_replica = 0
    request = _request(client, shard_scope=None, facade_scope=None, only_shard=only_shard)
    request = replace(
        request,
        insert_column_types=None,
        ch_creation_policy=replace(request.ch_creation_policy, shard_engine=engine),
    )

    get_backend_adapter("ch").finalize_stage_table(request)

    assert client.tables[LOCAL, PHYSICAL].rows == STAGED_ROWS
    assert client.local_loads
    assert len(client.swaps) == (2 if only_shard else 4)
    assert all(scope == LOCAL for scope, _left, _right in client.swaps)
    assert not any(
        "ON CLUSTER" in sql for sql, _ in client.commands if "SYSTEM SYNC REPLICA" not in sql
    )


def test_local_ddl_cannot_report_success_for_a_remote_distributed_destination() -> None:
    client = ReplacementClient(storage=LOCAL)
    remote = Table(23, ("remote_column",))
    client.tables["core", PHYSICAL] = remote
    local = client.tables[LOCAL, PHYSICAL]
    request = _request(client, shard_scope=None, facade_scope=None)
    request = replace(
        request, ch_creation_policy=replace(request.ch_creation_policy, distributed_cluster="core")
    )

    before = dict(client.tables)
    with pytest.raises(InvalidSqlInputError, match="Distributed replacement routing differs"):
        get_backend_adapter("ch").finalize_stage_table(request)

    assert client.tables["core", PHYSICAL] == remote
    assert client.tables[LOCAL, PHYSICAL] == local
    assert client.tables == before
    assert client.tables[LOCAL, STAGE].rows == STAGED_ROWS
    assert client.swaps == []
    assert client.loads == []
    assert client.commands == []


@pytest.mark.parametrize("rows", [[], [(1,)]])
def test_nonreplicated_facade_rejects_unverifiable_cluster_metadata(rows) -> None:
    connection = SimpleNamespace(query=lambda _sql: SimpleNamespace(result_rows=rows))
    with pytest.raises(SqlConfigError, match="verify replica configuration"):
        _validate_nonreplicated_facade_cluster(connection, "core")


def test_nonreplicated_facade_allows_one_replica_on_a_routed_connection() -> None:
    connection = wrap_client(
        SimpleNamespace(query=lambda _sql: SimpleNamespace(result_rows=[(1, 1, True)])),
        SimpleNamespace(cluster_routing=ChClusterRouting("core"), database="sandbox"),
    )
    _validate_nonreplicated_facade_cluster(connection, "core")


@pytest.mark.parametrize("engines", [[], [("Atomic",), ("Ordinary",)]])
def test_remote_replace_rejects_missing_or_inconsistent_database_engines(engines) -> None:
    client = SimpleNamespace(query=lambda _sql: SimpleNamespace(result_rows=engines))

    with pytest.raises(RuntimeError, match="consistent destination database engines"):
        _database_engine_on_scope(client, "sandbox", "core")
