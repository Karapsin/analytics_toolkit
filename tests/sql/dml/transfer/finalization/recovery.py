from __future__ import annotations

from typing import Any

import pandas as pd
import pytest
from analytics_toolkit.sql.backends.ch.creation_policy import ClickHouseCreationPolicy
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from analytics_toolkit.sql.connection.errors import AmbiguousSqlReplaceError
from analytics_toolkit.sql.dml.transfer.flow import finalize
from analytics_toolkit.sql.dml.transfer.runtime.models import (
    TransferConnectionRefs,
    TransferOptions,
    TransferStageState,
)
from sqlglot import exp, parse_one

from tests.sql._support.assertions import strip_query_label
from tests.sql._support.ch_replace import (
    LOCAL,
    PHYSICAL,
    STAGE,
    STAGED_ROWS,
    TARGET,
    ReplacementClient,
)


@pytest.fixture
def replacement(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[ReplacementClient, TransferOptions, TransferStageState]:
    for name in (
        "_drop_incomplete_fresh_target",
        "_validate_fresh_target_row_count",
        "_analyze_final_target",
        "get_sql_connection",
    ):
        monkeypatch.setattr(
            finalize,
            name,
            lambda *_a, **_k: pytest.fail(
                "failed existing-target replacement must not use "
                "fresh-target or analysis operations"
            ),
        )
    client = ReplacementClient()
    client.bad_final_count = True
    options = TransferOptions(
        from_db_key="gp",
        from_db_backend="gp",
        to_db_key="ch",
        to_db_backend="ch",
        source_sql="SELECT id, value FROM sandbox.source",
        target_table=TARGET,
        write_mode="replace",
        replace_target_table=True,
        retry_cnt=2,
        timeout_increment=0,
        ch_sharding_key="cityHash64(id)",
        query_label="replacement recovery",
        regular_ch_policy=ClickHouseCreationPolicy(
            create_distributed_pair=True,
            shard_engine="ReplicatedMergeTree",
            shard_on_cluster="core",
            distributed_engine_template=(
                "Distributed({cluster}, {database}, {shard_table}, {sharding_key})"
            ),
            distributed_cluster="core",
            distributed_on_cluster=None,
            sharding_key="cityHash64(id)",
            ddl_ready_timeout_seconds=1,
            ddl_wait_policy="wait_none",
        ),
    )
    state = TransferStageState(
        target_exists=True,
        target_existed_at_start=True,
        stage_table=STAGE,
        stage_table_created=True,
        first_non_empty_batch=pd.DataFrame({"id": [1], "value": ["new"]}),
        stage_column_types={"id": "Int64", "value": "String"},
        source_columns=["id", "value"],
    )
    return client, options, state


def _finalize(
    client: ReplacementClient,
    options: TransferOptions,
    state: TransferStageState,
) -> None:
    def run_on_target(_role: str, operation: Any) -> Any:
        return operation({"connection": client})

    finalize.finalize_loaded_stage(
        options,
        TransferConnectionRefs(),
        state,
        STAGED_ROWS,
        target_connection_runner=run_on_target,
    )


def _drop_resource(statement: str) -> tuple[str, str | None]:
    drop = parse_one(strip_query_label(statement), read=ClickHouseDialect)
    assert isinstance(drop, exp.Drop)
    table = drop.this or drop.args["tables"][0]
    cluster = drop.find(exp.OnCluster)
    return f"{table.db}.{table.name}", cluster.this.name if cluster is not None else None


def test_final_count_failure_restores_existing_target_through_finalization(
    replacement: tuple[ReplacementClient, TransferOptions, TransferStageState],
) -> None:
    client, options, state = replacement
    original_tables = dict(client.tables)
    original_facades = dict(client.facade_sources)

    with pytest.raises(RuntimeError, match="final target row count"):
        _finalize(client, options, state)

    assert client.tables == original_tables
    assert all(client.tables[key] is table for key, table in original_tables.items())
    assert client.facade_sources == original_facades
    assert state.target_exists is True
    assert state.target_existed_at_start is True
    assert state.target_created_by_operation is False
    assert len(client.loads) == 1
    destination, source = client.loads[0]
    assert destination[0] == "core"
    assert destination[1].startswith(f"{PHYSICAL}__replace_")
    assert source == (LOCAL, STAGE)
    assert client.local_loads == []
    assert len(client.swaps) == 4
    assert client.swaps[2:] == list(reversed(client.swaps[:2]))
    assert [(scope, left) for scope, left, _right in client.swaps[:2]] == [
        ("core", PHYSICAL),
        (LOCAL, TARGET),
    ]
    drops = [sql for sql, _settings in client.commands if "DROP TABLE" in sql]
    assert len(drops) == 2
    assert all("__replace_" in sql for sql in drops)
    resources = [_drop_resource(statement) for statement in drops]
    assert sum(scope == "core" for _table, scope in resources) == 1
    assert sum(scope is None for _table, scope in resources) == 1
    assert all(
        table.startswith((PHYSICAL if scope == "core" else TARGET) + "__replace_")
        for table, scope in resources
    )
    counts = [sql for sql in client.queries if "query_label=replacement recovery" in sql]
    assert len(counts) == 2
    assert all("FROM cluster('core', 'sandbox', 'events_shard" in sql for sql in counts)
    assert "FROM cluster('core', 'sandbox', 'events_shard')" in counts[-1]


def test_failed_rollback_preserves_artifacts_through_finalization(
    replacement: tuple[ReplacementClient, TransferOptions, TransferStageState],
) -> None:
    client, options, state = replacement
    original_physical = client.tables["core", PHYSICAL]
    original_facade = client.tables[LOCAL, TARGET]
    unrelated_facade = client.tables["core", TARGET]
    original_route = client.facade_sources[LOCAL, TARGET]
    client.fail_rollback = True

    with pytest.raises(AmbiguousSqlReplaceError, match="automatic rollback") as exc_info:
        _finalize(client, options, state)

    assert isinstance(exc_info.value.__cause__, RuntimeError)
    assert "final target row count" in str(exc_info.value.__cause__)
    assert str(exc_info.value.rollback_error) == "rollback unavailable"
    remote_artifacts = {
        name: table
        for (scope, name), table in client.tables.items()
        if scope == "core" and "__replace_" in name
    }
    assert len(remote_artifacts) == 1
    physical_name, physical_artifact = next(iter(remote_artifacts.items()))
    assert physical_name.startswith(PHYSICAL + "__replace_")
    assert physical_artifact is original_physical
    local_artifacts = {
        name: table
        for (scope, name), table in client.tables.items()
        if scope == LOCAL and "__replace_" in name
    }
    assert len(local_artifacts) == 1
    facade_name, facade_artifact = next(iter(local_artifacts.items()))
    assert facade_name.startswith(TARGET + "__replace_")
    assert facade_artifact is original_facade
    assert client.facade_sources[LOCAL, facade_name] == original_route
    assert len(remote_artifacts) + len(local_artifacts) == 2
    assert client.tables["core", TARGET] is unrelated_facade
    assert client.tables["core", PHYSICAL].rows == STAGED_ROWS
    assert client.tables[LOCAL, STAGE].rows == STAGED_ROWS
    assert len(client.loads) == 1
    assert client.loads[0][0][0] == "core"
    assert client.loads[0][1] == (LOCAL, STAGE)
    assert client.local_loads == []
    assert len(client.swaps) == 2
    assert [(scope, left) for scope, left, _right in client.swaps] == [
        ("core", PHYSICAL),
        (LOCAL, TARGET),
    ]
    assert not any("DROP" in sql for sql, _settings in client.commands)
    assert state.target_created_by_operation is False


def test_next_finalization_refuses_unresolved_recovery_before_mutation(
    replacement: tuple[ReplacementClient, TransferOptions, TransferStageState],
) -> None:
    client, options, state = replacement
    original_tables = dict(client.tables)
    client.fail_rollback = True
    with pytest.raises(AmbiguousSqlReplaceError, match="automatic rollback"):
        _finalize(client, options, state)
    assert all(
        any(table is original for table in client.tables.values())
        for original in original_tables.values()
    )
    retained_tables = dict(client.tables)
    retained_facades = dict(client.facade_sources)
    commands = list(client.commands)
    loads = list(client.loads)
    swaps = list(client.swaps)
    client.bad_final_count = False
    client.fail_rollback = False
    client.rollback_pending = False

    with pytest.raises(AmbiguousSqlReplaceError, match="unresolved artifact"):
        _finalize(client, options, state)

    assert client.tables == retained_tables
    assert all(client.tables[key] is table for key, table in retained_tables.items())
    assert client.facade_sources == retained_facades
    assert client.commands == commands
    assert client.loads == loads
    assert client.swaps == swaps
    assert len(loads) == 1
    assert client.local_loads == []
    assert state.target_created_by_operation is False
