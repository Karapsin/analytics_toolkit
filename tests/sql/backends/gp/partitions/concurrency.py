from __future__ import annotations

from tests.sql._support.partitions import (
    FakeDbapiConnection,
    _stub_leaf_partition_discovery,
    gp_maintenance_module,
    pytest,
)


def test_gp_analyze_partitioned_table_schedules_later_concurrent_partitions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    partition_names = [
        "analytics.orders_1_prt_a",
        "analytics.orders_1_prt_b",
        "analytics.orders_1_prt_c",
    ]
    _stub_leaf_partition_discovery(monkeypatch, partition_names)
    connections = [FakeDbapiConnection() for _ in partition_names]
    monkeypatch.setattr(
        gp_maintenance_module,
        "get_sql_connection",
        lambda _key: connections.pop(0),
    )

    gp_maintenance_module.gp_analyze_partitioned_table(
        "gp",
        "analytics.orders",
        partition_names,
        concurrency=2,
        retry_cnt=1,
        timeout_increment=0,
    )

    assert connections == []
