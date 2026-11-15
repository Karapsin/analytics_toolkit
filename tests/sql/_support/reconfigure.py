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
from tests.sql._support.reconfigure_metadata import ReconfigureMetadata

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
        self.query_settings: list[object] = []
        self.metadata_macros = {"cluster": "core", "copy_route": "archive"}
        self.metadata_overrides: dict[tuple[str, str], str] = {}
        self.metadata = ReconfigureMetadata(self)
        self.closed = False

    def query(self, query: str, settings: object = None) -> FakeClickHouseResult:
        self.queries.append(query)
        self.query_settings.append(settings)
        ddl = self.metadata.show_create(query) or self._show_create_ddl(query)
        if ddl is not None:
            return FakeClickHouseResult([(ddl,)])
        if query.startswith("SELECT engine FROM system.databases"):
            return FakeClickHouseResult([(self.database_engine,)])
        if query.startswith("EXISTS TABLE "):
            return FakeClickHouseResult([(1,)])
        if (rows := self.metadata.rows(query)) is not None:
            return FakeClickHouseResult(rows)
        if query.startswith("SELECT count() FROM "):
            return FakeClickHouseResult([(3,)])
        return FakeClickHouseResult([])

    def metadata_definitions(self) -> dict[tuple[str, str], str]:
        definitions = {
            ("analytics", table): ddl
            for table in ("events", "events_shard")
            if (ddl := self._show_create_ddl(f"SHOW CREATE TABLE analytics.{table}")) is not None
        }
        return {**definitions, **self.metadata_overrides}

    @staticmethod
    def _show_create_ddl(query: str) -> str | None:
        return {
            "SHOW CREATE TABLE analytics.events": TABLE_DDL,
            "SHOW CREATE TABLE analytics.events_shard": SHARD_DDL,
        }.get(query)

    def command(self, query: str, settings: object = None) -> None:
        self.commands.append(query)
        self.command_settings.append(settings)
        self.metadata.command(query)

    def close(self) -> None:
        self.closed = True


class CountingReconfigureClient(ReconfigureClient):
    def __init__(self, counts: list[int]) -> None:
        super().__init__()
        self.counts = counts

    def query(self, query: str, settings: object = None) -> FakeClickHouseResult:
        if query.startswith("SELECT count() FROM ") and self.counts:
            self.queries.append(query)
            return FakeClickHouseResult([(self.counts.pop(0),)])
        return super().query(query, settings=settings)


class LocalReconfigureClient(ReconfigureClient):
    def metadata_definitions(self) -> dict[tuple[str, str], str]:
        return {
            **super().metadata_definitions(),
            ("analytics", "local_events"): self.metadata_overrides.get(
                ("analytics", "local_events"), LOCAL_DDL
            ),
        }

    def query(self, query: str, settings: object = None) -> FakeClickHouseResult:
        if query == "SHOW CREATE TABLE analytics.local_events":
            self.queries.append(query)
            self.query_settings.append(settings)
            ddl = self.metadata.show_create(query) or self.metadata_overrides.get(
                ("analytics", "local_events"), LOCAL_DDL
            )
            return FakeClickHouseResult([(ddl,)])
        return super().query(query, settings=settings)


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
