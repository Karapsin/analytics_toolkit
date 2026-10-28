from __future__ import annotations

from typing import Any

import pandas as pd
import pytest
from analytics_toolkit.sql.backends.ch.creation_policy import ClickHouseCreationPolicy
from analytics_toolkit.sql.connection.errors import AmbiguousSqlReplaceError
from analytics_toolkit.sql.dml.transfer.flow import finalize
from analytics_toolkit.sql.dml.transfer.runtime.models import (
    TransferConnectionRefs,
    TransferOptions,
    TransferStageState,
)

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
            distributed_on_cluster="core",
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


def test_final_count_failure_restores_existing_target_through_finalization(
    replacement: tuple[ReplacementClient, TransferOptions, TransferStageState],
) -> None:
    client, options, state = replacement
    original_tables = dict(client.tables)
    original_facades = dict(client.facade_sources)

    with pytest.raises(RuntimeError, match="final target row count"):
        _finalize(client, options, state)

    assert client.tables == original_tables
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
    assert all(scope == "core" for scope, _left, _right in client.swaps)
    drops = [sql for sql, _settings in client.commands if "DROP TABLE" in sql]
    assert len(drops) == 3
    assert all("__replace_" in sql for sql in drops)
    assert sum("ON CLUSTER" in sql for sql in drops) == 2
    counts = [sql for sql in client.queries if "query_label=replacement recovery" in sql]
    assert len(counts) == 2
    assert all("FROM cluster('core', 'sandbox', 'events_shard" in sql for sql in counts)
    assert "FROM cluster('core', 'sandbox', 'events_shard')" in counts[-1]


def test_failed_rollback_preserves_artifacts_through_finalization(
    replacement: tuple[ReplacementClient, TransferOptions, TransferStageState],
) -> None:
    client, options, state = replacement
    original_physical = client.tables["core", PHYSICAL]
    original_facade = client.tables["core", TARGET]
    client.fail_rollback = True

    with pytest.raises(AmbiguousSqlReplaceError, match="automatic rollback") as exc_info:
        _finalize(client, options, state)

    assert isinstance(exc_info.value.__cause__, OSError)
    assert str(exc_info.value.__cause__) == "rollback unavailable"
    remote_artifacts = {
        name: table
        for (scope, name), table in client.tables.items()
        if scope == "core" and "__replace_" in name
    }
    assert len(remote_artifacts) == 2
    assert original_physical in remote_artifacts.values()
    assert original_facade in remote_artifacts.values()
    local_artifacts = {
        name: table
        for (scope, name), table in client.tables.items()
        if scope == LOCAL and "__replace_" in name
    }
    assert len(local_artifacts) == 1
    assert all(table.rows == 0 for table in local_artifacts.values())
    assert client.tables["core", PHYSICAL].rows == STAGED_ROWS
    assert client.tables[LOCAL, STAGE].rows == STAGED_ROWS
    assert len(client.loads) == 1
    assert client.loads[0][0][0] == "core"
    assert client.loads[0][1] == (LOCAL, STAGE)
    assert client.local_loads == []
    assert len(client.swaps) == 2
    assert all(scope == "core" for scope, _left, _right in client.swaps)
    assert not any("DROP" in sql for sql, _settings in client.commands)
    assert state.target_created_by_operation is False


def test_next_finalization_refuses_unresolved_recovery_before_mutation(
    replacement: tuple[ReplacementClient, TransferOptions, TransferStageState],
) -> None:
    client, options, state = replacement
    client.fail_rollback = True
    with pytest.raises(AmbiguousSqlReplaceError, match="automatic rollback"):
        _finalize(client, options, state)
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
    assert client.facade_sources == retained_facades
    assert client.commands == commands
    assert client.loads == loads
    assert client.swaps == swaps
    assert len(loads) == 1
    assert client.local_loads == []
    assert state.target_created_by_operation is False
