"""Disposable checks that dataframe column validation precedes destructive loads."""

from __future__ import annotations

import os
import uuid
from typing import TYPE_CHECKING

import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends import get_backend_adapter

from tests.sql.integration._support.backends import (
    BACKENDS,
    backend_alias,
    backend_enabled,
    integration_table,
)
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


def _load_options(backend: str) -> dict[str, Any]:
    if backend == "gp":
        return {"gp_distributed_by_key": "row_id"}
    if backend == "ch":
        return {
            "ch_engine": "MergeTree",
            "ch_distributed_table": False,
            "ch_shard_on_cluster": "integration_cluster",
            "order_by": "row_id",
        }
    return {}


def _rows(alias: str, table: str) -> list[tuple[Any, ...]]:
    frame = sql.read(
        alias,
        f"SELECT row_id, payload FROM {table} ORDER BY row_id",
        retry_cnt=1,
        query_label=_label("load-column-rows"),
    )
    return list(frame.itertuples(index=False, name=None))


@pytest.mark.parametrize(
    "backend",
    [scenario_param(f"audit.load.unknown_columns.{backend}", backend) for backend in BACKENDS],
)
def test_unknown_dataframe_columns_are_rejected_before_truncating_existing_rows(
    backend: str,
    monkeypatch: pytest.MonkeyPatch,
    resource_registry: ResourceRegistry,
) -> None:
    if not backend_enabled(backend):
        pytest.skip("Greenplum requires x86_64")
    alias = backend_alias(backend, target=True)
    table = resource_registry.table(
        alias,
        integration_table(backend, "load_columns"),
        ch_cluster="integration_cluster" if backend == "ch" else None,
    )
    schema = (
        {"row_id": "Int64", "payload": "Nullable(String)"}
        if backend == "ch"
        else {"row_id": "BIGINT", "payload": "TEXT" if backend == "gp" else "VARCHAR"}
    )
    options = _load_options(backend)
    seeded_rows = [(1, "one"), (2, "two")]
    assert (
        sql.load_df(
            alias,
            table,
            pd.DataFrame(seeded_rows, columns=["row_id", "payload"]),
            table_schema=schema,
            write_mode="replace",
            retry_cnt=1,
            query_label=_label("load-column-seed"),
            **options,
        )
        == 2
    )
    assert _rows(alias, table) == seeded_rows
    adapter = get_backend_adapter(backend)
    execute_command, insert_dataframe_batch = (
        adapter.execute_command,
        adapter.insert_dataframe_batch,
    )
    command_submissions: list[str] = []
    insert_submissions: list[str] = []

    def record_command(connection: Any, statement: str) -> Any:
        command_submissions.append(statement)
        return execute_command(connection, statement)

    def record_insert(connection: Any, table_name: str, batch: Any, **kwargs: Any) -> Any:
        insert_submissions.append(table_name)
        return insert_dataframe_batch(connection, table_name, batch, **kwargs)

    with monkeypatch.context() as target_operations:
        # These observers delegate real commands and native dataframe insertion.
        # A rollback or later cleanup cannot erase evidence of an early clear.
        target_operations.setattr(adapter, "execute_command", record_command)
        target_operations.setattr(adapter, "insert_dataframe_batch", record_insert)
        with pytest.raises(
            ValueError,
            match=r"^Incoming column\(s\) have no authoritative SQL type: extra$",
        ):
            sql.load_df(
                alias,
                table,
                pd.DataFrame({"row_id": [99], "extra": ["unexpected"]}),
                write_mode="truncate_insert",
                retry_cnt=1,
                query_label=_label("load-column-reject"),
                **options,
            )
    assert command_submissions == []
    assert insert_submissions == []
    assert _rows(alias, table) == seeded_rows

    with monkeypatch.context() as target_operations:
        target_operations.setattr(adapter, "execute_command", record_command)
        target_operations.setattr(adapter, "insert_dataframe_batch", record_insert)
        for frame in (
            pd.DataFrame({"payload": ["third"], "row_id": [3]}),
            pd.DataFrame({"row_id": [4]}),
        ):
            assert (
                sql.load_df(
                    alias,
                    table,
                    frame,
                    write_mode="append",
                    key_columns="row_id",
                    retry_cnt=1,
                    query_label=_label("load-column-valid-append"),
                    **options,
                )
                == 1
            )
    assert command_submissions
    assert insert_submissions
    assert _rows(alias, table) == [*seeded_rows, (3, "third"), (4, None)]
