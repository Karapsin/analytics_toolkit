"""Disposable checks of ClickHouse insertion policy before destructive loading."""

from __future__ import annotations

import os
import uuid
from typing import TYPE_CHECKING

import analytics_toolkit.general as general_module
import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends import get_backend_adapter

from tests.sql.integration._support.backends import integration_table
from tests.sql.integration._support.identity import query_label
from tests.sql.integration.manifest import scenario_param

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path
    from typing import Any

    from tests.sql.integration._support.resources import ResourceRegistry

pytestmark = [pytest.mark.integration, pytest.mark.integration_core]


def _label(purpose: str) -> str:
    return query_label(
        os.environ.get("SQL_INTEGRATION_RUN_ID", uuid.uuid4().hex[:8]),
        os.environ.get("SQL_INTEGRATION_TEST_ID", "manual"),
        purpose,
    )


def _set_materialized_setting(
    connections: dict[str, dict[str, object]],
    write_connections: Callable[[dict[str, dict[str, object]]], Path],
    *,
    allowed: bool,
) -> None:
    config = connections["ch"]
    previous = config.get("settings")
    connections["ch"] = {
        **config,
        "settings": {
            **(previous if isinstance(previous, dict) else {}),
            "insert_allow_materialized_columns": int(allowed),
        },
    }
    general_module.set_connections_path(write_connections(connections))


def _computed_rows(alias: str, table: str) -> list[tuple[Any, ...]]:
    frame = sql.read(
        alias,
        f"SELECT row_id, payload, computed FROM {table} ORDER BY row_id",
        retry_cnt=1,
        query_label=_label("computed-column-rows"),
    )
    return list(frame.itertuples(index=False, name=None))


@pytest.mark.parametrize(
    "kind",
    [
        scenario_param(f"audit.load.computed_columns.{kind.lower()}", kind)
        for kind in ("MATERIALIZED", "ALIAS")
    ],
)
def test_noninsertable_computed_columns_are_rejected_before_clearing_rows(
    kind: str,
    monkeypatch: pytest.MonkeyPatch,
    resource_registry: ResourceRegistry,
    integration_connections: dict[str, dict[str, object]],
    write_sql_connections: Callable[[dict[str, dict[str, object]]], Path],
) -> None:
    alias = "ch"
    native = integration_connections[alias]["driver"] == "native"
    _set_materialized_setting(integration_connections, write_sql_connections, allowed=False)
    table = resource_registry.table(alias, integration_table("ch", f"computed_{kind.lower()}"))
    ephemeral = resource_registry.table(alias, integration_table("ch", "ephemeral_input"))
    sql.execute(
        alias,
        f"CREATE TABLE {table} (row_id Int64, payload String, "
        f"computed Int64 {kind} row_id + 10) ENGINE=MergeTree ORDER BY row_id",
        retry_cnt=1,
        query_label=_label("computed-column-create"),
    )
    sql.execute(
        alias,
        f"INSERT INTO {table} (row_id, payload) VALUES (1, 'one'), (2, 'two')",
        retry_cnt=1,
        query_label=_label("computed-column-seed"),
    )
    seeded_rows = [(1, "one", 11), (2, "two", 12)]
    assert _computed_rows(alias, table) == seeded_rows
    adapter = get_backend_adapter("ch")
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

    with monkeypatch.context() as operations:
        operations.setattr(adapter, "execute_command", record_command)
        operations.setattr(adapter, "insert_dataframe_batch", record_insert)
        with pytest.raises(
            ValueError,
            match=r"^Incoming column\(s\) cannot be inserted explicitly: computed$",
        ):
            sql.load_df(
                alias,
                table,
                pd.DataFrame({"row_id": [99], "payload": ["unexpected"], "computed": [999]}),
                write_mode="truncate_insert",
                retry_cnt=1,
                query_label=_label("computed-column-reject"),
            )
    assert commands == []
    assert inserts == []
    assert _computed_rows(alias, table) == seeded_rows

    if native and kind == "MATERIALIZED":
        # Only the native wrapper forwards explicit materialized-column types.
        _set_materialized_setting(integration_connections, write_sql_connections, allowed=True)
        assert (
            sql.load_df(
                alias,
                table,
                pd.DataFrame({"row_id": [3], "payload": ["native-opt-in"], "computed": [333]}),
                write_mode="append",
                retry_cnt=1,
                query_label=_label("computed-native-opt-in"),
            )
            == 1
        )
        assert _computed_rows(alias, table) == [*seeded_rows, (3, "native-opt-in", 333)]

    sql.execute(
        alias,
        f"CREATE TABLE {ephemeral} (row_id Int64, raw_input String EPHEMERAL, "
        "stored String DEFAULT concat(raw_input, '-stored')) ENGINE=MergeTree ORDER BY row_id",
        retry_cnt=1,
        query_label=_label("ephemeral-column-create"),
    )
    sql.execute(
        alias,
        f"INSERT INTO {ephemeral} (row_id, raw_input) VALUES (1, 'seed')",
        retry_cnt=1,
        query_label=_label("ephemeral-column-seed"),
    )
    with monkeypatch.context() as operations:
        operations.setattr(adapter, "execute_command", record_command)
        operations.setattr(adapter, "insert_dataframe_batch", record_insert)
        assert (
            sql.load_df(
                alias,
                ephemeral,
                pd.DataFrame({"row_id": [2], "raw_input": ["fresh"]}),
                write_mode="truncate_insert",
                retry_cnt=1,
                query_label=_label("ephemeral-column-load"),
            )
            == 1
        )
    assert commands
    assert inserts
    actual = sql.read(
        alias,
        f"SELECT row_id, stored FROM {ephemeral}",
        retry_cnt=1,
        query_label=_label("ephemeral-column-check"),
    )
    assert list(actual.itertuples(index=False, name=None)) == [(2, "fresh-stored")]
