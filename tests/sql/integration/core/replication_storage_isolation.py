"""Disposable replicated rebuilds reject sharing the original Keeper storage."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.connection.errors import InvalidSqlInputError

from tests.sql.integration._support.reconfiguration import (
    create_owned,
    inventory,
    label,
    literal,
    quoted,
    resource_name,
)
from tests.sql.integration.manifest import scenario_param

if TYPE_CHECKING:
    from tests.sql.integration._support.resources import ResourceRegistry

pytestmark = [pytest.mark.integration, pytest.mark.integration_core]
_CORE = "integration_cluster"
_ARCHIVE = "integration_archive_cluster"
_DATABASE = "integration"


@pytest.mark.parametrize(
    ("alias", "operation"),
    [
        scenario_param(
            f"audit.clickhouse.replication_storage_isolation.{operation}.{alias}",
            alias,
            operation,
        )
        for operation in ("replace", "relocate")
        for alias in ("ch", "ch_native")
    ],
)
def test_replicated_rebuild_never_joins_the_live_source_storage(
    resource_registry: ResourceRegistry, alias: str, operation: str
) -> None:
    relation = resource_name(f"replication_isolation_{alias}_{operation}")
    physical = relation + "_shard"
    table, shard = quoted(_DATABASE, relation), quoted(_DATABASE, physical)
    prefix = "/clickhouse/audit/" + relation
    path = prefix if operation == "replace" else prefix + "/{table}"
    engine = f"ReplicatedMergeTree({literal(path)}, '{{replica}}')"
    create_owned(
        resource_registry,
        f"CREATE TABLE {shard} ON CLUSTER {quoted(_CORE)} (id UInt64) ENGINE={engine} ORDER BY id",
        f"DROP TABLE IF EXISTS {shard} ON CLUSTER {quoted(_CORE)} SYNC",
    )
    create_owned(
        resource_registry,
        f"CREATE TABLE {table} ON CLUSTER {quoted(_CORE)} (id UInt64) "
        f"ENGINE=Distributed({literal(_CORE)}, {literal(_DATABASE)}, {literal(physical)}, rand())",
        f"DROP TABLE IF EXISTS {table} ON CLUSTER {quoted(_CORE)} SYNC",
    )
    sql.execute(
        alias,
        f"INSERT INTO {table} VALUES (11), (12), (13)",
        retry_cnt=1,
        query_label=label("replication-isolation-seed"),
    )
    before = inventory(_CORE, [_DATABASE], relation)
    before_rows = sql.read(alias, f"SELECT id FROM {table} ORDER BY id", retry_cnt=1).id.tolist()
    assert before_rows == [11, 12, 13]
    desired = f"ReplicatedMergeTree({literal(path)}, 'new_{{replica}}')"

    def rebuild() -> None:
        if operation == "replace":
            sql.load_df(
                alias,
                table,
                pd.DataFrame({"id": pd.Series([201, 202], dtype="uint64")}),
                table_schema={"id": "UInt64"},
                write_mode="replace",
                ch_engine=desired,
                ch_distributed_table=True,
                ch_shard_on_cluster=_CORE,
                ch_distributed_cluster=_CORE,
                ch_distributed_on_cluster=_CORE,
                order_by="id",
                retry_cnt=1,
                timeout_increment=0,
                query_label=label("replication-isolation-replace"),
            )
        else:
            sql.ch_reconfigure_table(
                alias,
                table,
                ch_engine=desired,
                ch_shard_on_cluster=_ARCHIVE,
                ch_distributed_cluster=_ARCHIVE,
                ch_distributed_on_cluster=_CORE,
                retry_cnt=1,
                timeout_increment=0,
                query_label=label("replication-isolation-relocate"),
            )

    with pytest.raises(InvalidSqlInputError, match="replication storage overlaps"):
        rebuild()

    sql.execute(
        alias,
        f"SYSTEM SYNC REPLICA ON CLUSTER {quoted(_CORE)} {shard}",
        retry_cnt=1,
        query_label=label("replication-isolation-barrier"),
    )
    assert inventory(_CORE, [_DATABASE], relation) == before
    assert (
        sql.read(alias, f"SELECT id FROM {table} ORDER BY id", retry_cnt=1).id.tolist()
        == before_rows
    )
