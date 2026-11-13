"""Actual Trino partition strings cannot be confused with dry-plan markers."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends import get_backend_adapter
from sqlglot import exp, parse_one

from tests.sql._support.assertions import strip_query_label
from tests.sql._support.fakes import FakeDbapiConnection, FakeDbapiCursor
from tests.sql._support.load_table import load_df_module

_TARGET = "iceberg.sandbox.target"
_TEMPLATES = (
    "DELETE FROM {table} WHERE {partition_column} = {partition_value}",
    "DELETE FROM {table} WHERE {partition_column} IS NOT DISTINCT FROM {partition_value}",
)


class _PartitionCursor(FakeDbapiCursor):
    def execute(self, statement: str, params: tuple[Any, ...] | None = None) -> None:
        super().execute(statement, params)
        body = strip_query_label(statement)
        if "information_schema.columns" in body:
            self._rows = [("id", "bigint"), ("month", "varchar")]
        elif body.startswith("SELECT DISTINCT"):
            self._rows = [(value,) for value in self.connection.partitions]
        else:
            self._rows = []
        if body.startswith(f"DELETE FROM {_TARGET} "):
            parsed = parse_one(body, read="trino")
            literal = parsed.args["where"].this.expression
            assert isinstance(literal, exp.Literal)
            assert literal.is_string
            self.connection.deleted_partitions.append(literal.this)


class _PartitionConnection(FakeDbapiConnection):
    def __init__(self) -> None:
        super().__init__()
        self.partitions: list[Any] = []
        self.deleted_partitions: list[str] = []
        self.batches: list[pd.DataFrame] = []

    def cursor(self) -> _PartitionCursor:
        return _PartitionCursor(self)


def _load_boundaries(monkeypatch, write_sql_connections, template):
    write_sql_connections(
        {
            "trino": {
                "type": "trino",
                "host": "trino.example",
                "port": 8080,
                "user": "user",
                "password": "password",
                "catalog": "iceberg",
                "schema": "sandbox",
                "upsert_partition_drop_sql_template": template,
            }
        }
    )
    connection = _PartitionConnection()
    stages = iter(("scratch.incoming", "scratch.payload"))

    def insert(_backend, _connection, _table, frame, **_kwargs):
        connection.batches.append(frame.copy())
        connection.partitions = list(dict.fromkeys(frame["month"].tolist()))
        return len(frame)

    monkeypatch.setattr(load_df_module, "get_sql_connection", lambda _key: connection)
    monkeypatch.setattr(load_df_module, "table_exists", lambda *_a, **_k: True)
    monkeypatch.setattr(load_df_module, "create_stage_table", lambda **_kwargs: next(stages))
    monkeypatch.setattr(load_df_module, "insert_table_batch", insert)
    monkeypatch.setattr(load_df_module, "validate_stage_uniqueness", lambda **_kwargs: None)
    monkeypatch.setattr(load_df_module, "analyze_table", lambda *_a, **_k: None)
    monkeypatch.setattr(load_df_module, "cleanup_stage_table_with_retry", lambda *_a, **_k: None)
    return connection


@pytest.mark.parametrize("template", _TEMPLATES)
@pytest.mark.parametrize(
    "partition", ["<part>", "<affected partition value>", "ordinary", r"path\name'quoted"]
)
def test_load_df_upsert_quotes_actual_partition_strings(
    monkeypatch, write_sql_connections, template, partition
):
    connection = _load_boundaries(monkeypatch, write_sql_connections, template)
    count = sql.load_df(
        "trino",
        _TARGET,
        pd.DataFrame({"id": [1], "month": [partition]}),
        write_mode="upsert",
        key_columns="id",
        upsert_partition_column="month",
        retry_cnt=1,
        timeout_increment=0,
    )
    assert count == 1
    assert connection.batches[0]["month"].tolist() == [partition]
    assert connection.deleted_partitions == [partition]
    assert any('SELECT DISTINCT "month" FROM scratch.incoming' in s for s in connection.executed)
    assert any(s.startswith(f"INSERT INTO {_TARGET} ") for s in connection.executed)


def test_load_df_upsert_rejects_null_partition_keys_before_stage_mutation(
    monkeypatch, write_sql_connections
):
    connection = _load_boundaries(monkeypatch, write_sql_connections, _TEMPLATES[1])
    with pytest.raises(ValueError, match=r"Null key values found.*month"):
        sql.load_df(
            "trino",
            _TARGET,
            pd.DataFrame({"id": [1], "month": [None]}),
            write_mode="upsert",
            key_columns="id",
            upsert_partition_column="month",
            retry_cnt=1,
            timeout_increment=0,
        )
    assert not connection.batches
    assert not connection.partitions
    assert not connection.deleted_partitions


@pytest.mark.parametrize("template", _TEMPLATES)
def test_unknown_partition_values_keep_only_the_synthetic_dry_plan_marker(template):
    adapter = get_backend_adapter("trino")
    options = {"partition_column": "month", "trino_partition_drop_sql_template": template}
    statements = adapter.build_drop_upsert_partition_sqls(_TARGET, partition_values=None, **options)
    assert statements == [
        template.format(
            table=_TARGET, partition_column='"month"', partition_value="<affected partition value>"
        )
    ]
    assert "'<affected partition value>'" not in statements[0]
    assert adapter.build_drop_upsert_partition_sqls(_TARGET, partition_values=[], **options) == []


def test_actual_native_and_null_partition_literals_preserve_template_semantics():
    adapter = get_backend_adapter("trino")
    statements = adapter.build_drop_upsert_partition_sqls(
        _TARGET,
        partition_column="month",
        partition_values=[None, False, 17, 1.25, date(2026, 1, 2), Decimal("12.30")],
        trino_partition_drop_sql_template=_TEMPLATES[1],
    )
    assert [statement.rsplit(" FROM ", 1)[1] for statement in statements] == [
        "NULL",
        "FALSE",
        "17",
        "1.25",
        "'2026-01-02'",
        "'12.30'",
    ]
