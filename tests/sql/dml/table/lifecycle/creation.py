from __future__ import annotations

from tests.sql._support.lifecycle import (
    LifecycleAdapter,
    maintenance,
)


def test_clear_ch_distributed_table_data_builds_and_executes_both_commands(
    lifecycle_adapter: LifecycleAdapter,
) -> None:
    maintenance.clear_ch_distributed_table_data(
        "connection",
        "db.target",
        ch_cluster="cluster",
        query_label="q",
    )

    assert [call[0] for call in lifecycle_adapter.calls] == [
        "build_clear_target_sqls",
        "execute_commands",
    ]
    assert lifecycle_adapter.calls[1][1][1] == [
        "TRUNCATE shard",
        "TRUNCATE distributed",
    ]
