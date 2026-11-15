"""Full native ownership state for the staged-load replacement contracts."""

from __future__ import annotations

from types import SimpleNamespace

from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from sqlglot import exp, parse_one

from tests.sql._support.assertions import strip_query_label
from tests.sql._support.ch_replace import ReplacementClient, Table
from tests.sql._support.load_table import TEST_CH_SHARD_TABLE, TEST_CH_STAGE_TABLE, TEST_CH_TABLE


class LoadTableReplacementClient:
    def __init__(self, stage_column_types):
        self.target = f"default.{TEST_CH_TABLE}"
        self.physical = f"default.{TEST_CH_SHARD_TABLE}"
        self.stage = TEST_CH_STAGE_TABLE
        self.state = ReplacementClient(storage="core", coordinator_scope="core")
        self.state.current_database = "default"
        self.state.engines["core"] = "Ordinary"
        columns = ("id", "score")
        self.state.tables = {
            ("core", self.target): Table(0, columns, replicated=False),
            ("core", self.physical): Table(17, columns, replicated=False),
            ("core", self.stage): Table(1, tuple(stage_column_types), replicated=False),
        }
        self.state.column_types = {
            ("core", self.target): ("UInt64", "Int64"),
            ("core", self.physical): ("UInt64", "Int64"),
            ("core", self.stage): tuple(stage_column_types.values()),
        }
        self.state.facade_sources = {("core", self.target): ("core", self.physical)}
        self.commands = []
        self.command_settings = []
        self.queries = []
        self.query_settings = []

    def command(self, statement, settings=None):
        self.commands.append(statement)
        self.command_settings.append(dict(settings or {}))
        self.state.command(statement, settings=settings)

    def query(self, statement, settings=None, **kwargs):
        self.queries.append(statement)
        self.query_settings.append(dict(settings or {}))
        clean = strip_query_label(statement)
        if clean.startswith("SHOW CREATE TABLE "):
            table = parse_one(
                f"SELECT * FROM {clean.removeprefix('SHOW CREATE TABLE ')}",
                read=ClickHouseDialect,
            ).find(exp.Table)
            assert table is not None
            qualified = table.copy()
            if not qualified.db:
                qualified.set("db", exp.to_identifier(self.state.current_database))
            name = self.state.table_name(qualified)
            return SimpleNamespace(result_rows=[(self.state.metadata.ddl(("core", name)),)])
        return self.state.query(statement, settings=settings, **kwargs)

    def assert_replaced(self, expected_columns, expected_types):
        physical = self.state.tables["core", self.physical]
        expected_schema = tuple(
            exp.DataType.build(native, dialect=ClickHouseDialect) for native in expected_types
        )
        assert physical.rows == 1
        assert physical.columns == tuple(expected_columns)
        assert (
            tuple(
                exp.DataType.build(native, dialect=ClickHouseDialect)
                for native in self.state.column_types["core", self.physical]
            )
            == expected_schema
        )
        assert self.state.tables["core", self.target].columns == tuple(expected_columns)
        assert (
            tuple(
                exp.DataType.build(native, dialect=ClickHouseDialect)
                for native in self.state.column_types["core", self.target]
            )
            == expected_schema
        )
        assert self.state.facade_sources["core", self.target] == ("core", self.physical)
        assert self.state.tables["core", self.stage].rows == 1
        assert set(self.state.tables) == {
            ("core", self.target),
            ("core", self.physical),
            ("core", self.stage),
        }
        membership = [
            settings
            for query, settings in zip(self.queries, self.query_settings)
            if "FROM system.clusters" in query and "host_address" in query
        ]
        assert membership
        assert all(settings == {"skip_unavailable_shards": 0} for settings in membership)
