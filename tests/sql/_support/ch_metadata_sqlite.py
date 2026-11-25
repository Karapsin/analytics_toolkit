"""Execute generated metadata relational SQL against an in-memory boundary.

Only ClickHouse table-function sources and tuple transport are translated;
filter, grouping, ordering and join placement come from the actual adapter SQL.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

import pandas as pd
from analytics_toolkit.sql.backends.ch.cluster_functions import cluster_arguments
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from sqlglot import exp, parse_one


class _ArgMin:
    def __init__(self) -> None:
        self.best: Any = None
        self.value: Any = None

    def step(self, value: Any, weight: Any) -> None:
        if weight is not None and (self.best is None or weight < self.best):
            self.best, self.value = weight, value

    def finalize(self) -> Any:
        return self.value


class _UniqExact:
    def __init__(self) -> None:
        self.values: set[Any] = set()

    def step(self, value: Any) -> None:
        if value is not None:
            self.values.add(value)

    def finalize(self) -> int:
        return len(self.values)


class MetadataServer:
    def __init__(self, members: list[tuple[int, int]], tables: list[tuple[Any, ...]]) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.queries: list[str] = []
        self.errors: list[str] = []
        self.connection.create_aggregate("arg_min", 2, _ArgMin)
        self.connection.create_function("atk_tuple", -1, lambda *values: json.dumps(values))
        self.connection.create_function("struct", -1, lambda *values: json.dumps(values))
        self.connection.create_function(
            "starts_with", 2, lambda value, prefix: value.startswith(prefix)
        )
        self.connection.create_function(
            "tupleElement", 2, lambda value, i: json.loads(value)[i - 1]
        )
        self.connection.create_aggregate("uniqExact", 1, _UniqExact)
        self.connection.execute("CREATE TABLE topology(cluster, shard_num, replica_num)")
        self.connection.executemany("INSERT INTO topology VALUES ('core', ?, ?)", members)
        self.connection.execute(
            "CREATE TABLE host_tables(_shard_num, database, name, total_rows, total_bytes, "
            "engine, engine_full)"
        )
        self.connection.executemany("INSERT INTO host_tables VALUES (?, ?, ?, ?, ?, ?, ?)", tables)
        self.connection.execute(
            "CREATE TABLE host_replicas(_shard_num, database, table_name, "
            "is_session_expired, is_readonly)"
        )
        self.connection.executemany(
            "INSERT INTO host_replicas VALUES (?, ?, ?, 0, 0)",
            [(row[0], row[1], row[2]) for row in tables if row[5].startswith("Replicated")],
        )

    def read(self, _key: str, query: str) -> pd.DataFrame:
        self.queries.append(query)
        tree = parse_one(query, read=ClickHouseDialect)
        for table in list(tree.find_all(exp.Table)):
            if isinstance(table.this, exp.Anonymous):
                _cluster, database, relation, _trailing = cluster_arguments(table.this, "db")
            elif table.db:
                database, relation = table.db, table.name
            else:
                continue
            if database != "system":
                continue
            names = {"tables": "host_tables", "clusters": "topology", "replicas": "host_replicas"}
            if relation in names:
                table.set("this", exp.to_identifier(names[relation]))
                table.set("db", None)
                table.set("catalog", None)
        for value in list(tree.find_all(exp.Tuple)):
            value.replace(exp.Anonymous(this="atk_tuple", expressions=value.expressions))
        for column in tree.find_all(exp.Column):
            if column.name == "table":
                column.set("this", exp.to_identifier("table_name"))
        statement = tree.sql(dialect="sqlite")
        try:
            cursor = self.connection.execute(statement)
            rows = cursor.fetchall()
        except sqlite3.Error as exc:
            self.errors.append(f"{exc}: {statement}")
            raise
        return pd.DataFrame(rows, columns=[item[0] for item in cursor.description], dtype=object)

    def close(self) -> None:
        self.connection.close()
