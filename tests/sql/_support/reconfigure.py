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
from analytics_toolkit.sql.connection.ddl_defaults import (
    ClickHouseDistributedDefaults,
    ClickHouseObjectDefaults,
    ClickHouseScopeDefaults,
)
from analytics_toolkit.sql.connection.errors import (
    InvalidSqlInputError,
    SqlConfigError,
    UnsupportedConnectionTypeError,
)
from analytics_toolkit.sql.execution.plans import SqlOperationResult, SqlPlan
from sqlglot import exp

from tests.sql._support.fakes import FakeClickHouseResult

reconfigure_api = importlib.import_module("analytics_toolkit.sql.dml.table.ch_reconfigure")

reconfigure_backend = importlib.import_module("analytics_toolkit.sql.backends.ch.reconfigure")

reconfigure_ddl = importlib.import_module("analytics_toolkit.sql.backends.ch.reconfigure_ddl")

reconfigure_execution = importlib.import_module(
    "analytics_toolkit.sql.backends.ch.reconfigure_execution"
)

reconfigure_policy = importlib.import_module("analytics_toolkit.sql.backends.ch.reconfigure_policy")

reconfigure_support = importlib.import_module(
    "analytics_toolkit.sql.backends.ch.reconfigure_support"
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
        "partition_by": None,
        "order_by": None,
        "ch_sharding_key": None,
        "ch_distributed_table": None,
        "ch_distributed_engine_template": None,
        "ch_distributed_cluster": None,
        "ch_shard_on_cluster": None,
        "ch_distributed_on_cluster": None,
        "ch_settings": None,
        "reset_partition_by": False,
        "reset_order_by": False,
        "to_defaults": False,
        "regular_defaults": None,
        "validate_row_count": True,
        "query_label": None,
    }
    values.update(overrides)
    return ChReconfigureOptions(**values)  # type: ignore[arg-type]


def _regular_defaults() -> ClickHouseScopeDefaults:
    return ClickHouseScopeDefaults(
        create_distributed_pair=True,
        shard=ClickHouseObjectDefaults("MergeTree", "core"),
        distributed=ClickHouseDistributedDefaults(
            "Distributed({cluster}, {database}, {shard_table}, {sharding_key})",
            "core",
            "{cluster}",
            "cityHash64(id)",
        ),
    )


__all__ = [
    "LOCAL_DDL",
    "SHARD_DDL",
    "TABLE_DDL",
    "ChReconfigureOptions",
    "ClickHouseDistributedDefaults",
    "ClickHouseObjectDefaults",
    "ClickHouseScopeDefaults",
    "CountingReconfigureClient",
    "FakeClickHouseResult",
    "InvalidSqlInputError",
    "LocalReconfigureClient",
    "ReconfigureClient",
    "SimpleNamespace",
    "SqlConfigError",
    "SqlOperationResult",
    "SqlPlan",
    "UnsupportedConnectionTypeError",
    "_options",
    "_regular_defaults",
    "exp",
    "get_backend_adapter",
    "importlib",
    "plan_ch_table_reconfiguration",
    "pytest",
    "reconfigure_api",
    "reconfigure_backend",
    "reconfigure_ddl",
    "reconfigure_execution",
    "reconfigure_policy",
    "reconfigure_support",
    "sql",
]
