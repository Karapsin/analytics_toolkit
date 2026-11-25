"""Disposable checks that existing NOT NULL constraints precede target clearing."""

from __future__ import annotations

import os
import re
import uuid
from typing import TYPE_CHECKING

import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends import get_backend_adapter

from tests.sql.integration._support.backends import (
    backend_alias,
    backend_enabled,
    integration_table,
)
from tests.sql.integration._support.identity import query_label
from tests.sql.integration._support.normalization import normalize_scalar
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


def _rows(alias: str, table: str, backend: str) -> list[tuple[Any, ...]]:
    defaulted = ", defaulted_value" if backend == "gp" else ""
    frame = sql.read(
        alias,
        f"SELECT row_id, required_value, nullable_value{defaulted} FROM {table} ORDER BY row_id",
        retry_cnt=1,
        query_label=_label("not-null-rows"),
    )
    return [
        tuple(normalize_scalar(value) for value in row) for row in frame.itertuples(index=False)
    ]


@pytest.mark.parametrize(
    "backend",
    [scenario_param(f"audit.load.not_null.{backend}", backend) for backend in ("gp", "trino")],
)
def test_existing_not_null_constraints_reject_invalid_rows_before_truncation(
    backend: str,
    monkeypatch: pytest.MonkeyPatch,
    resource_registry: ResourceRegistry,
) -> None:
    if not backend_enabled(backend):
        pytest.skip("Greenplum requires x86_64")
    alias = backend_alias(backend, target=True)
    table = resource_registry.table(alias, integration_table(backend, "not_null"))
    defaulted = ", defaulted_value BIGINT NOT NULL DEFAULT 7" if backend == "gp" else ""
    distribution = " DISTRIBUTED BY (row_id)" if backend == "gp" else ""
    sql.execute(
        alias,
        f"CREATE TABLE {table} (row_id BIGINT NOT NULL, required_value BIGINT NOT NULL, "
        f"nullable_value BIGINT{defaulted}){distribution}",
        retry_cnt=1,
        query_label=_label("not-null-create"),
    )
    sql.execute(
        alias,
        f"INSERT INTO {table} (row_id, required_value, nullable_value) "
        "VALUES (1, 10, 100), (2, 20, NULL)",
        retry_cnt=1,
        query_label=_label("not-null-seed"),
    )
    seeded = (
        [(1, 10, 100, 7), (2, 20, None, 7)]
        if backend == "gp"
        else [
            (1, 10, 100),
            (2, 20, None),
        ]
    )
    assert _rows(alias, table, backend) == seeded
    invalid = [
        (
            pd.DataFrame({"row_id": [99], "required_value": [None], "nullable_value": [99]}),
            "Incoming NULL value(s) violate authoritative NOT NULL column(s): required_value",
        ),
        (
            pd.DataFrame({"row_id": [99], "nullable_value": [99]}),
            "Incoming data omits required NOT NULL column(s): required_value",
        ),
    ]
    if backend == "gp":
        invalid.append(
            (
                pd.DataFrame({"row_id": [99], "required_value": [99], "defaulted_value": [None]}),
                "Incoming NULL value(s) violate authoritative NOT NULL column(s): defaulted_value",
            )
        )
    adapter = get_backend_adapter(backend)
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

    options = {"gp_distributed_by_key": "row_id"} if backend == "gp" else {}
    with monkeypatch.context() as operations:
        operations.setattr(adapter, "execute_command", record_command)
        operations.setattr(adapter, "insert_dataframe_batch", record_insert)
        for frame, message in invalid:
            with pytest.raises(ValueError, match=f"^{re.escape(message)}$"):
                sql.load_df(
                    alias,
                    table,
                    frame,
                    write_mode="truncate_insert",
                    retry_cnt=1,
                    query_label=_label("not-null-reject"),
                    **options,
                )
            assert commands == []
            assert inserts == []
            assert _rows(alias, table, backend) == seeded

        assert (
            sql.load_df(
                alias,
                table,
                pd.DataFrame({"required_value": [30], "row_id": [3]}),
                write_mode="append",
                retry_cnt=1,
                query_label=_label("not-null-valid-omission"),
                **options,
            )
            == 1
        )
        supplied = {"row_id": [4], "required_value": [0], "nullable_value": [0]}
        if backend == "gp":
            supplied["defaulted_value"] = [0]
        assert (
            sql.load_df(
                alias,
                table,
                pd.DataFrame(supplied),
                write_mode="append",
                retry_cnt=1,
                query_label=_label("not-null-valid-zero"),
                **options,
            )
            == 1
        )
        expected_append = (
            [(3, 30, None, 7), (4, 0, 0, 0)]
            if backend == "gp"
            else [
                (3, 30, None),
                (4, 0, 0),
            ]
        )
        assert _rows(alias, table, backend) == [*seeded, *expected_append]
        valid = pd.DataFrame(
            {
                "row_id": [1, 2],
                "required_value": [10, 20],
                "nullable_value": pd.Series([100, None], dtype=object),
            }
        )
        assert (
            sql.load_df(
                alias,
                table,
                valid,
                write_mode="truncate_insert",
                retry_cnt=1,
                query_label=_label("not-null-valid-truncate"),
                **options,
            )
            == 2
        )
    assert commands
    assert inserts
    assert _rows(alias, table, backend) == seeded
