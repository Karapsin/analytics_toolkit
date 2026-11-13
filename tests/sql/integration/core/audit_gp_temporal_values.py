"""Disposable native Greenplum TIME/INTERVAL dataframe validation checks."""

from __future__ import annotations

import datetime as dt
import os
import uuid
from decimal import Decimal
from typing import TYPE_CHECKING

import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends import get_backend_adapter

from tests.sql.integration._support.backends import backend_enabled, integration_table
from tests.sql.integration._support.identity import query_label
from tests.sql.integration.manifest import scenario_param

if TYPE_CHECKING:
    from typing import Any

    from tests.sql.integration._support.resources import ResourceRegistry

pytestmark = [pytest.mark.integration, pytest.mark.integration_core]


def _label(purpose: str) -> str:
    return query_label(
        os.environ.get("SQL_INTEGRATION_RUN_ID", uuid.uuid4().hex[:8]),
        os.environ.get("SQL_INTEGRATION_TEST_ID", "manual"),
        purpose,
    )


def _values(family: str) -> list[Any]:
    if family == "time":
        return [
            None,
            dt.time(),
            None,
            dt.time(12, 30, 15, 123456),
            dt.time(23, 59, 59, 999999),
            None,
        ]
    return [
        None,
        dt.timedelta(microseconds=-1),
        None,
        dt.timedelta(days=2, seconds=3, microseconds=4),
        dt.timedelta(days=-2, seconds=7, microseconds=123456),
        None,
    ]


def _microseconds(value: dt.time | dt.timedelta | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, dt.time):
        seconds = value.hour * 3600 + value.minute * 60 + value.second
        return seconds * 1000000 + value.microsecond
    return (value.days * 86400 + value.seconds) * 1000000 + value.microseconds


def _rows(alias: str, table: str) -> list[tuple[int, int | None]]:
    frame = sql.read(
        alias,
        "SELECT row_id, payload IS NULL AS missing, "
        "((EXTRACT(EPOCH FROM payload))::NUMERIC(30, 6) * 1000000)::NUMERIC(30, 0) "
        f"AS exact_microseconds FROM {table} ORDER BY row_id",
        retry_cnt=1,
        query_label=_label("gp-temporal-rows"),
    )
    rows = []
    for row_id, missing, value in frame.itertuples(index=False, name=None):
        if bool(missing):
            assert pd.isna(value)
            exact = None
        else:
            assert isinstance(value, Decimal)
            assert value == value.to_integral_value()
            exact = int(value)
        rows.append((int(row_id), exact))
    return rows


@pytest.mark.parametrize(
    "family",
    [scenario_param(f"audit.load.gp_temporal.{family}", family) for family in ("time", "interval")],
)
def test_greenplum_native_time_and_interval_existing_and_explicit_loads(
    family: str,
    monkeypatch: pytest.MonkeyPatch,
    resource_registry: ResourceRegistry,
) -> None:
    if not backend_enabled("gp"):
        pytest.skip("Greenplum requires x86_64")
    alias = "gp_target"
    native_type = "TIME(6)" if family == "time" else "INTERVAL(6)"
    existing = resource_registry.table(alias, integration_table("gp", f"{family}_existing"))
    explicit = resource_registry.table(alias, integration_table("gp", f"{family}_explicit"))
    sql.execute(
        alias,
        f"CREATE TABLE {existing} (row_id BIGINT NOT NULL, payload {native_type}) "
        "DISTRIBUTED BY (row_id)",
        retry_cnt=1,
        query_label=_label("gp-temporal-create"),
    )
    seed = "TIME '00:00:01'" if family == "time" else "INTERVAL '1 second'"
    sql.execute(
        alias,
        f"INSERT INTO {existing} (row_id, payload) VALUES (99, {seed})",
        retry_cnt=1,
        query_label=_label("gp-temporal-seed"),
    )
    assert _rows(alias, existing) == [(99, 1000000)]

    adapter = get_backend_adapter("gp")
    execute_command, insert_dataframe_batch = (
        adapter.execute_command,
        adapter.insert_dataframe_batch,
    )
    commands: list[str] = []
    inserts: list[str] = []

    def record_command(connection: Any, statement: str) -> Any:
        commands.append(statement)
        return execute_command(connection, statement)

    def record_insert(connection: Any, table_name: str, batch: Any, **kwargs: Any) -> Any:
        inserts.append(table_name)
        return insert_dataframe_batch(connection, table_name, batch, **kwargs)

    values = _values(family)
    incoming = pd.DataFrame(
        {"row_id": range(1, len(values) + 1), "payload": pd.Series(values, dtype=object)}
    )
    original = incoming.copy(deep=True)
    with monkeypatch.context() as operations:
        operations.setattr(adapter, "execute_command", record_command)
        operations.setattr(adapter, "insert_dataframe_batch", record_insert)
        invalid = pd.DataFrame(
            {"row_id": [100], "payload": pd.Series([{"invalid": family}], dtype=object)}
        )
        with pytest.raises(ValueError, match=r"TIME|INTERVAL|time|interval"):
            sql.load_df(
                alias,
                existing,
                invalid,
                write_mode="truncate_insert",
                gp_distributed_by_key="row_id",
                retry_cnt=1,
                query_label=_label("gp-temporal-reject"),
            )
        assert commands == []
        assert inserts == []
        assert _rows(alias, existing) == [(99, 1000000)]

        assert sql.load_df(
            alias,
            existing,
            incoming,
            write_mode="truncate_insert",
            gp_distributed_by_key="row_id",
            retry_cnt=1,
            query_label=_label("gp-temporal-existing"),
        ) == len(incoming)
        assert sql.load_df(
            alias,
            explicit,
            incoming,
            write_mode="replace",
            table_schema={"row_id": "BIGINT", "payload": native_type},
            gp_distributed_by_key="row_id",
            retry_cnt=1,
            query_label=_label("gp-temporal-explicit"),
        ) == len(incoming)
    assert commands
    assert inserts
    expected = [(index, _microseconds(value)) for index, value in enumerate(values, start=1)]
    assert _rows(alias, existing) == expected
    assert _rows(alias, explicit) == expected
    pd.testing.assert_frame_equal(incoming, original)
