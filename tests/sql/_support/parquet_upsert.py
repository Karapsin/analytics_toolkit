"""Hive stage lifecycle over real Arrow bytes and actual emitted Trino SQL."""

from __future__ import annotations

import io
from urllib.parse import urlsplit

import pyarrow as pa
import pyarrow.parquet as pq
from sqlglot import exp, parse_one

from tests.sql._support.load_table import load_df_module
from tests.sql._support.transfer_not_null import TransferDatabase
from tests.sql._support.trino_upsert_payload import SCHEMA, UpsertDatabase, _UpsertConnection


class ParquetUpsertDatabase(UpsertDatabase):
    target_key = ("iceberg", "sandbox", "events")

    def __init__(self):
        super().__init__()
        self.tables[self.target_key] = self.tables.pop(("sandbox", "events"))
        self.files = {}
        self.cleaned_locations = []
        self.created_sql = {}
        self.lose_create_ack = None
        self.arrow_writes = []
        self.hive_drop_failure = None
        self.failed_hive_drops = []

    @property
    def target(self):
        return self.tables.get(self.target_key, self.tables.get(("sandbox", "events")))

    def identity(self, node):
        if isinstance(node, exp.Schema):
            node = node.this
        return node.catalog or "iceberg", node.db or "sandbox", node.name

    def metadata(self, statement, params):
        if "information_schema" not in statement:
            return None
        tree = parse_one(statement, read="trino")
        table = next(item for item in tree.find_all(exp.Table) if item.db == "information_schema")
        identity = (table.catalog or "iceberg", *params)
        if table.name == "tables":
            return [(1,)] if identity in self.tables else []
        return self.column_metadata(statement, self.tables[identity])

    def execute(self, connection, statement, params=None):
        tree = parse_one(statement, read="trino")
        table = tree.find(exp.Table)
        identity = self.identity(table) if table is not None else None
        if (
            isinstance(tree, exp.Drop)
            and identity[0] == "hive"
            and self.hive_drop_failure is not None
        ):
            self.failed_hive_drops.append(identity)
            raise self.hive_drop_failure
        if identity == self.target_key and isinstance(
            tree, (exp.Create, exp.Drop, exp.Delete, exp.TruncateTable, exp.Insert)
        ):
            self.live_mutations.append(statement)
        result = TransferDatabase.execute(self, connection, statement, params)
        if isinstance(tree, exp.Create):
            self.created_schemas.append((identity, dict(self.tables[identity].schema)))
            self.created_sql[identity] = statement
            if identity[0] == "hive" and self.lose_create_ack is not None:
                error, self.lose_create_ack = self.lose_create_ack, None
                raise error
        return result

    def location(self, identity):
        tree = parse_one(self.created_sql[identity], read="trino")
        locations = [
            value.this
            for value in tree.find_all(exp.Literal)
            if value.is_string and urlsplit(value.this).scheme == "s3"
        ]
        assert len(locations) == 1
        return locations[0]


def parquet_upsert_target(monkeypatch, write_sql_connections):
    write_sql_connections(
        {
            "trino": {
                "type": "trino",
                "host": "first.invalid",
                "port": 8080,
                "user": "fake",
                "password": "fake",
                "catalog": "iceberg",
                "schema": "sandbox",
                "transfer_staging_schema": "iceberg.sandbox",
                "s3_transfer_staging_schema": "hive.scratch",
                "s3_transfer_staging_location": "s3://unit-bucket/load-stages",
                "upsert_partition_drop_sql_template": (
                    "DELETE FROM {table} WHERE {partition_column} "
                    "IS NOT DISTINCT FROM {partition_value}"
                ),
            }
        }
    )
    database = ParquetUpsertDatabase()

    def connect(_alias):
        connection = _UpsertConnection(database)
        database.opened.append(connection)
        return connection

    def write(frame, *, stage_external_location, column_types, on_progress=None, **_options):
        assert dict(column_types) == SCHEMA
        identity = database.created_schemas[-1][0]
        assert identity[0] == "hive"
        assert database.location(identity) == stage_external_location
        schema = pa.schema(
            [("id", pa.int64()), ("month", pa.string()), ("value", pa.timestamp("us"))]
        )
        table = pa.Table.from_pydict({name: list(frame[name]) for name in SCHEMA}, schema=schema)
        stream = io.BytesIO()
        pq.write_table(table, stream)
        payload = stream.getvalue()
        database.files[stage_external_location] = payload
        database.tables[identity].rows = pq.read_table(io.BytesIO(payload)).to_pylist()
        database.arrow_writes.append((identity, schema, len(frame)))
        if on_progress is not None:
            on_progress(len(frame))
        return len(frame)

    def cleanup(location, **_options):
        database.cleaned_locations.append(location)
        database.files.pop(location, None)

    monkeypatch.setattr(load_df_module, "get_sql_connection", connect)
    monkeypatch.setattr(
        load_df_module, "ensure_parquet_staging_dependencies", lambda: (pa, pq, None)
    )
    monkeypatch.setattr(load_df_module, "write_dataframe_to_parquet_stage", write)
    monkeypatch.setattr(load_df_module, "cleanup_parquet_stage_location", cleanup)
    return database
