from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pandas as pd
import pytest
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.ch.creation_policy import ClickHouseCreationPolicy
from analytics_toolkit.sql.backends.ch.routing import ChClusterRouting, wrap_client
from analytics_toolkit.sql.backends.ch.safe_replace import _database_engine_on_scope
from analytics_toolkit.sql.backends.models import StageFinalizationRequest
from analytics_toolkit.sql.connection.errors import AmbiguousSqlReplaceError

from tests.sql._support.ch_replace import (
    LOCAL,
    PHYSICAL,
    STAGE,
    STAGED_ROWS,
    TARGET,
    ReplacementClient,
    Table,
)


def _request(connection, *, shard_scope="core", facade_scope="core", only_shard=False):
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
            distributed_cluster=shard_scope,
            distributed_on_cluster=facade_scope,
            sharding_key="cityHash64(id)",
            ddl_ready_timeout_seconds=1,
            ddl_wait_policy="wait_none",
        ),
    )


@pytest.mark.parametrize("native", [False, True], ids=["http", "native"])
@pytest.mark.parametrize("only_shard", [False, True], ids=["pair", "shard"])
@pytest.mark.parametrize(
    ("engine", "data_function"),
    [("ReplicatedMergeTree", "cluster"), ("MergeTree", "clusterAllReplicas")],
)
def test_replace_loads_remote_storage_from_local_stage(
    native, only_shard, engine, data_function
) -> None:
    client = ReplacementClient()
    client.is_native_transport = native
    original_local = Table(23, ("unrelated_local_column",))
    client.tables[LOCAL, PHYSICAL] = original_local

    request = _request(client, only_shard=only_shard)
    request = replace(
        request, ch_creation_policy=replace(request.ch_creation_policy, shard_engine=engine)
    )
    get_backend_adapter("ch").finalize_stage_table(request)

    assert client.tables["core", PHYSICAL] == Table(
        STAGED_ROWS, ("id", "value"), replicated=engine == "ReplicatedMergeTree"
    )
    assert client.tables[LOCAL, PHYSICAL] == original_local
    assert client.loads[0][0][0] == "core"
    assert client.loads[0][1] == (LOCAL, STAGE)
    assert client.local_loads == []
    assert all(scope == "core" for scope, _left, _right in client.swaps)
    assert len(client.swaps) == (1 if only_shard else 2)
    assert not any("__replace_" in table or "__backup_" in table for _scope, table in client.tables)
    insert, settings = next(
        (sql, settings) for sql, settings in client.commands if "INSERT INTO" in sql
    )
    assert f"FUNCTION {data_function}('core', 'sandbox', 'events_shard__replace_" in insert
    assert "cityHash64(id)" in insert
    assert "(`id`, `value`) SELECT CAST(`id` AS Int64)" in insert
    assert "query_label=replacement topology" in insert
    assert settings["distributed_foreground_insert"] == 1
    counts = [sql for sql in client.queries if sql.startswith("/* analytics_toolkit")]
    assert len(counts) == 2
    assert all(f"FROM {data_function}('core', 'sandbox', 'events_shard" in sql for sql in counts)
    assert any("DROP TABLE" in sql and "ON CLUSTER" not in sql for sql, _ in client.commands)


def test_replace_preserves_stage_replica_routing_without_changing_storage_scope() -> None:
    client = ReplacementClient(stage_scope="ingest")
    connection = wrap_client(
        client,
        SimpleNamespace(cluster_routing=ChClusterRouting("ingest"), database="sandbox"),
    )

    get_backend_adapter("ch").finalize_stage_table(_request(connection))

    assert client.tables["core", PHYSICAL].rows == STAGED_ROWS
    assert client.loads[0][1] == ("ingest", STAGE)
    insert = next(sql for sql, _ in client.commands if "INSERT INTO" in sql)
    assert "INSERT INTO FUNCTION cluster('core'," in insert
    assert "FROM clusterAllReplicas('ingest', 'stage'," in insert
    assert all(scope == "core" for scope, _left, _right in client.swaps)
    assert not any("ON CLUSTER 'ingest'" in sql for sql, _ in client.commands)


@pytest.mark.parametrize("only_shard", [False, True], ids=["pair", "shard"])
def test_replace_legacy_cluster_creates_remote_physical_before_loading(only_shard) -> None:
    client = ReplacementClient()
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
        "CREATE TABLE IF NOT EXISTS sandbox.events_shard__replace_" in sql and "ON CLUSTER" in sql
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
    assert client.tables["core", TARGET].columns == ("old_column",)
    assert len(client.swaps) == (4 if bad_final else 0)
    if bad_final:
        assert client.swaps[2:] == list(reversed(client.swaps[:2]))
    assert all(scope == "core" for scope, _left, _right in client.swaps)
    assert not any("__replace_" in table for _scope, table in client.tables)


def test_remote_replace_failed_rollback_preserves_recovery_artifacts() -> None:
    client = ReplacementClient()
    client.bad_final_count = True
    client.fail_rollback = True

    with pytest.raises(AmbiguousSqlReplaceError, match="automatic rollback"):
        get_backend_adapter("ch").finalize_stage_table(_request(client))

    assert any(scope == "core" and "__replace_" in table for scope, table in client.tables)
    assert any(scope == LOCAL and "__replace_" in table for scope, table in client.tables)
    assert not any("DROP TABLE" in sql for sql, _ in client.commands)


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


@pytest.mark.parametrize("bad_final", [False, True])
def test_remote_replace_keeps_separate_facade_scope_and_engine(bad_final) -> None:
    client = ReplacementClient()
    client.tables["facades", TARGET] = client.tables.pop(("core", TARGET))
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


def test_local_replace_retains_local_loading_counting_and_rename() -> None:
    client = ReplacementClient(storage=LOCAL)
    request = _request(client, shard_scope=None, facade_scope=None)
    request = replace(request, insert_column_types=None)

    get_backend_adapter("ch").finalize_stage_table(request)

    assert client.tables[LOCAL, PHYSICAL].rows == STAGED_ROWS
    assert client.local_loads
    assert len(client.swaps) == 4
    assert all(scope == LOCAL for scope, _left, _right in client.swaps)
    assert not any("ON CLUSTER" in sql for sql, _ in client.commands)


@pytest.mark.parametrize("engines", [[], [("Atomic",), ("Ordinary",)]])
def test_remote_replace_rejects_missing_or_inconsistent_database_engines(engines) -> None:
    client = SimpleNamespace(query=lambda _sql: SimpleNamespace(result_rows=engines))

    with pytest.raises(RuntimeError, match="consistent destination database engines"):
        _database_engine_on_scope(client, "sandbox", "core")
