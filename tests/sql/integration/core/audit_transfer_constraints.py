"""Disposable transfer constraint checks using the configured native source snapshot."""

from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pandas as pd
import pytest
import sqlparse
from analytics_toolkit import sql
from analytics_toolkit.sql.backends import get_backend_adapter
from sqlglot import exp, parse_one

from tests.sql.integration._support.backends import (
    backend_alias,
    backend_enabled,
    integration_table,
)
from tests.sql.integration._support.identity import query_label
from tests.sql.integration._support.normalization import normalize_scalar
from tests.sql.integration.manifest import scenario_param

if TYPE_CHECKING:
    from collections.abc import Iterator
    from typing import Any

    from tests.sql.integration._support.resources import ResourceRegistry

pytestmark = [pytest.mark.integration, pytest.mark.integration_core]


def _label(purpose: str) -> str:
    return query_label(
        os.environ.get("SQL_INTEGRATION_RUN_ID", uuid.uuid4().hex[:8]),
        os.environ.get("SQL_INTEGRATION_TEST_ID", "manual"),
        purpose,
    )


def _identity(table: exp.Table) -> tuple[str, str, str]:
    return table.catalog, table.db, table.name


def _references(statement: exp.Expression, identity: tuple[str, str, str]) -> bool:
    return any(_identity(table) == identity for table in statement.find_all(exp.Table))


def _mutates_table(statement: str, identity: tuple[str, str, str], dialect: Any) -> bool:
    kinds = {item.get_type() for item in sqlparse.parse(statement)}
    if not kinds & {"INSERT", "DELETE", "UPDATE", "DROP", "TRUNCATE", "ALTER"}:
        return False
    parsed = parse_one(statement, read=dialect)
    destinations = parsed.expressions if isinstance(parsed, exp.TruncateTable) else [parsed.this]
    for destination in destinations:
        table = destination.this if isinstance(destination, exp.Schema) else destination
        if isinstance(table, exp.Table):
            resolved = table.catalog or identity[0], table.db or identity[1], table.name
            if resolved == identity:
                return True
    return False


@dataclass
class _Observed:
    materializations: list[str] = field(default_factory=list)
    reads: list[str] = field(default_factory=list)
    streams: list[str] = field(default_factory=list)
    live_reads: list[str] = field(default_factory=list)
    live_mutations: list[str] = field(default_factory=list)

    def assert_snapshot_used_once(self) -> None:
        assert len(self.materializations) == 1
        assert self.reads or self.streams
        assert self.live_reads == []


@contextmanager
def _observe_transfer(
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
    source: str,
    target: str,
) -> Iterator[_Observed]:
    source_adapter, target_adapter = get_backend_adapter("ch"), get_backend_adapter(backend)
    source_dialect, target_dialect = source_adapter.sqlglot_dialect, target_adapter.sqlglot_dialect
    source_identity = _identity(parse_one(source, into=exp.Table, read=source_dialect))
    target_identity = _identity(parse_one(target, into=exp.Table, read=target_dialect))
    source_command, source_read, source_stream = (
        source_adapter.execute_command,
        source_adapter.read_columns,
        source_adapter.iter_source_batches,
    )
    target_command = target_adapter.execute_command
    observed = _Observed()

    def record_source_command(connection: Any, statement: str) -> Any:
        parsed = parse_one(statement, read=source_dialect)
        if isinstance(parsed, (exp.Create, exp.Insert)) and _references(parsed, source_identity):
            observed.materializations.append(statement)
        return source_command(connection, statement)

    def record_source_read(connection: Any, query: str, **kwargs: Any) -> Any:
        observed.reads.append(query)
        parsed = parse_one(query, read=source_dialect)
        zero_limit = any(str(limit.expression) == "0" for limit in parsed.find_all(exp.Limit))
        if (
            isinstance(parsed, exp.Select)
            and not zero_limit
            and _references(parsed, source_identity)
        ):
            observed.live_reads.append(query)
        return source_read(connection, query, **kwargs)

    def record_source_stream(**kwargs: Any) -> Iterator[Any]:
        query = kwargs["query"]
        observed.streams.append(query)
        if _references(parse_one(query, read=source_dialect), source_identity):
            observed.live_reads.append(query)
        yield from source_stream(**kwargs)

    def record_target_command(connection: Any, statement: str) -> Any:
        if _mutates_table(statement, target_identity, target_dialect):
            observed.live_mutations.append(statement)
        return target_command(connection, statement)

    with monkeypatch.context() as operations:
        operations.setattr(source_adapter, "execute_command", record_source_command)
        operations.setattr(source_adapter, "read_columns", record_source_read)
        operations.setattr(source_adapter, "iter_source_batches", record_source_stream)
        operations.setattr(target_adapter, "execute_command", record_target_command)
        yield observed


def _target_rows(alias: str, table: str, backend: str) -> list[tuple[Any, ...]]:
    defaulted = ", defaulted_value" if backend == "gp" else ""
    frame = sql.read(
        alias,
        f"SELECT row_id, event_date, required_value, nullable_value{defaulted} "
        f"FROM {table} ORDER BY event_date, row_id",
        retry_cnt=1,
        query_label=_label("transfer-not-null-rows"),
    )
    return [
        (
            normalize_scalar(row[0]),
            pd.Timestamp(row[1]).date().isoformat(),
            *(normalize_scalar(value) for value in row[2:]),
        )
        for row in frame.itertuples(index=False, name=None)
    ]


def _seed_rows(backend: str) -> list[tuple[Any, ...]]:
    rows = [
        (1, "2026-01-01", 10, 100),
        (1, "2026-02-01", 20, 200),
        (2, "2026-02-01", 22, None),
        (9, "2026-03-01", 90, 900),
    ]
    return [(*row, 7) for row in rows] if backend == "gp" else rows


@pytest.mark.parametrize(
    ("backend", "mode"),
    [
        scenario_param(f"audit.transfer.not_null.{backend}.{mode}", backend, mode)
        for backend, mode in (
            ("gp", "truncate_insert"),
            ("trino", "truncate_insert"),
            ("trino", "upsert"),
        )
    ],
)
def test_transfer_not_null_validation_precedes_live_target_mutation(
    backend: str,
    mode: str,
    monkeypatch: pytest.MonkeyPatch,
    resource_registry: ResourceRegistry,
) -> None:
    if not backend_enabled(backend):
        pytest.skip("Greenplum requires x86_64")
    source_alias, target_alias = "ch_source", backend_alias(backend, target=True)
    source = resource_registry.table(source_alias, integration_table("ch", "constraint_source"))
    target = resource_registry.table(target_alias, integration_table(backend, "constraint_target"))
    sql.execute(
        source_alias,
        f"CREATE TABLE {source} (row_id Int64, event_date Date, required_value Nullable(Int64), "
        "nullable_value Nullable(Int64)) ENGINE=MergeTree ORDER BY row_id",
        retry_cnt=1,
        query_label=_label("transfer-not-null-source-create"),
    )
    sql.execute(
        source_alias,
        f"INSERT INTO {source} VALUES (1, '2026-02-01', 0, NULL), (3, '2026-02-01', NULL, 300)",
        retry_cnt=1,
        query_label=_label("transfer-not-null-source-seed"),
    )
    defaulted = ", defaulted_value BIGINT NOT NULL DEFAULT 7" if backend == "gp" else ""
    properties = " DISTRIBUTED BY (row_id)" if backend == "gp" else ""
    if mode == "upsert":
        properties = " WITH (partitioning = ARRAY['event_date'])"
    sql.execute(
        target_alias,
        f"CREATE TABLE {target} (row_id BIGINT NOT NULL, event_date DATE NOT NULL, "
        f"required_value BIGINT NOT NULL, nullable_value BIGINT{defaulted}){properties}",
        retry_cnt=1,
        query_label=_label("transfer-not-null-target-create"),
    )
    sql.execute(
        target_alias,
        f"INSERT INTO {target} (row_id, event_date, required_value, nullable_value) VALUES "
        "(1, DATE '2026-01-01', 10, 100), (1, DATE '2026-02-01', 20, 200), "
        "(2, DATE '2026-02-01', 22, NULL), (9, DATE '2026-03-01', 90, 900)",
        retry_cnt=1,
        query_label=_label("transfer-not-null-target-seed"),
    )
    seeded = _seed_rows(backend)
    assert _target_rows(target_alias, target, backend) == seeded
    options: dict[str, Any] = {
        "from_sql": f"SELECT row_id, event_date, required_value, nullable_value FROM {source}",
        "to_table": target,
        "write_mode": mode,
        "batch_size": 1,
        "min_batch_size": 1,
        "adaptive_batch_size": False,
        "target_rows_per_second": False,
        "concurrency": 1,
        "retry_cnt": 1,
        "full_retry_cnt": 1,
    }
    if backend == "gp":
        options["gp_distributed_by_key"] = "row_id"
    if mode == "upsert":
        options.update(key_columns="row_id", upsert_partition_column="event_date")

    with _observe_transfer(monkeypatch, backend, source, target) as observed:
        with pytest.raises(ValueError, match="NOT NULL"):
            sql.transfer(
                source_alias,
                target_alias,
                query_label=_label("transfer-not-null-reject"),
                **options,
            )
        observed.assert_snapshot_used_once()
        assert observed.live_mutations == []
    assert _target_rows(target_alias, target, backend) == seeded

    sql.execute(
        source_alias,
        f"TRUNCATE TABLE {source}",
        retry_cnt=1,
        query_label=_label("transfer-not-null-source-reset"),
    )
    sql.execute(
        source_alias,
        f"INSERT INTO {source} VALUES (1, '2026-02-01', 0, NULL), (3, '2026-02-01', 30, 300)",
        retry_cnt=1,
        query_label=_label("transfer-not-null-source-valid"),
    )
    with _observe_transfer(monkeypatch, backend, source, target) as observed:
        assert (
            sql.transfer(
                source_alias,
                target_alias,
                query_label=_label("transfer-not-null-valid"),
                **options,
            )
            == 2
        )
        observed.assert_snapshot_used_once()
        assert observed.live_mutations
    expected = [(1, "2026-02-01", 0, None), (3, "2026-02-01", 30, 300)]
    if backend == "gp":
        expected = [(*row, 7) for row in expected]
    if mode == "upsert":
        expected = [seeded[0], expected[0], seeded[2], expected[1], seeded[3]]
    assert _target_rows(target_alias, target, backend) == expected

    sql.execute(
        source_alias,
        f"TRUNCATE TABLE {source}",
        retry_cnt=1,
        query_label=_label("transfer-not-null-source-empty"),
    )
    with _observe_transfer(monkeypatch, backend, source, target) as observed:
        assert (
            sql.transfer(
                source_alias,
                target_alias,
                query_label=_label("transfer-not-null-empty"),
                **options,
            )
            == 0
        )
        observed.assert_snapshot_used_once()
        if mode == "upsert":
            assert observed.live_mutations == []
        else:
            assert observed.live_mutations
    assert _target_rows(target_alias, target, backend) == (expected if mode == "upsert" else [])
