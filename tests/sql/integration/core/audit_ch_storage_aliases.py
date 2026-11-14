"""Disposable native storage aliases under the existing CH transport matrix."""

from __future__ import annotations

import os
import uuid
from typing import TYPE_CHECKING

import analytics_toolkit.general as general_module
import pandas as pd
import pytest
from analytics_toolkit import sql

from tests.sql.integration._support.identity import query_label, resource_name

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path
    from typing import Any

    from tests.sql.integration._support.resources import ResourceRegistry

pytestmark = [pytest.mark.integration, pytest.mark.integration_core]

_UINT64_MAX = 2**64 - 1
_SCHEMA = {
    "row_id": "Int64",
    "aggregate_value": "SimpleAggregateFunction(sum, UInt64)",
    "point_value": "Point",
    "ring_value": "Ring",
    "events": "Nested(x UInt64, y String)",
}


def _identity() -> tuple[str, str]:
    return (
        os.environ.get("SQL_INTEGRATION_RUN_ID", uuid.uuid4().hex[:8]),
        os.environ.get("SQL_INTEGRATION_TEST_ID", "manual"),
    )


def _label(purpose: str) -> str:
    return query_label(*_identity(), purpose)


def _configure_nested(
    connections: dict[str, dict[str, object]],
    write_connections: Callable[[dict[str, dict[str, object]]], Path],
) -> bool:
    alias = connections["ch_target"]
    previous = alias.get("settings")
    connections["ch_target"] = {
        **alias,
        "settings": {
            **(previous if isinstance(previous, dict) else {}),
            "flatten_nested": 0,
        },
    }
    general_module.set_connections_path(write_connections(connections))
    return alias["driver"] == "native"


def _owned_table(alias: str, registry: ResourceRegistry) -> tuple[str, str]:
    name = resource_name(*_identity(), "storage_aliases")
    table = f"integration.{name}"
    acknowledged = False

    def cleanup() -> None:
        if acknowledged:
            sql.execute(
                alias,
                f"DROP TABLE IF EXISTS {table}",
                retry_cnt=1,
                query_label=_label("ch-storage-alias-cleanup"),
            )

    registry.finalizer(cleanup)
    sql.execute(
        alias,
        f"CREATE TABLE {table} (row_id Int64, "
        "aggregate_value SimpleAggregateFunction(sum, UInt64), "
        "point_value Point, ring_value Ring, events Nested(x UInt64, y String)) "
        "ENGINE = MergeTree ORDER BY row_id",
        retry_cnt=1,
        query_label=_label("ch-storage-alias-create"),
    )
    # Neither a collision nor an ambiguous CREATE submission owns this name.
    acknowledged = True
    return table, name


def _frame(*, native: bool) -> pd.DataFrame:
    nested = (
        [[(7, "a"), (_UINT64_MAX, "b")], []]
        if native
        else [[{"x": 7, "y": "a"}, {"x": _UINT64_MAX, "y": "b"}], []]
    )
    return pd.DataFrame(
        {
            "row_id": [1, 2],
            "aggregate_value": pd.Series([7, _UINT64_MAX], dtype=object),
            "point_value": pd.Series([(1.25, 2.5), (-3.5, 4.75)], dtype=object),
            "ring_value": pd.Series([[(1.25, 2.5), (3.5, 4.75), (1.25, 2.5)], []], dtype=object),
            "events": pd.Series(nested, dtype=object),
        }
    )


def _metadata(alias: str, name: str) -> dict[str, str]:
    frame = sql.read(
        alias,
        "SELECT name, type FROM system.columns "
        f"WHERE database = 'integration' AND table = '{name}' ORDER BY position",
        retry_cnt=1,
        query_label=_label("ch-storage-alias-metadata"),
    )
    return dict(frame.itertuples(index=False, name=None))


def _rows(alias: str, table: str) -> list[tuple[Any, ...]]:
    frame = sql.read(
        alias,
        "SELECT src.row_id, src.aggregate_value, "
        "tupleElement(src.point_value, 1), tupleElement(src.point_value, 2), "
        "arrayMap(p -> tupleElement(p, 1), src.ring_value), "
        "arrayMap(p -> tupleElement(p, 2), src.ring_value), "
        "arrayMap(item -> tupleElement(item, 'x'), src.events), "
        "arrayMap(item -> tupleElement(item, 'y'), src.events) "
        f"FROM {table} AS src ORDER BY src.row_id",
        retry_cnt=1,
        query_label=_label("ch-storage-alias-rows"),
    )
    return [
        (int(row_id), int(amount), x, y, list(rx), list(ry), [int(v) for v in nx], list(ny))
        for row_id, amount, x, y, rx, ry, nx, ny in frame.itertuples(index=False, name=None)
    ]


def _load(alias: str, table: str, frame: pd.DataFrame, *, explicit: bool) -> int:
    options: dict[str, Any] = {"table_schema": _SCHEMA} if explicit else {}
    return sql.load_df(
        alias,
        table,
        frame,
        write_mode="truncate_insert",
        retry_cnt=1,
        ch_distributed_table=False,
        order_by="row_id",
        query_label=_label("ch-storage-alias-load"),
        **options,
    )


@pytest.mark.sql_scenario("audit.load.clickhouse_storage_aliases")
def test_clickhouse_storage_aliases_preserve_native_rows_and_unflattened_metadata(
    resource_registry: ResourceRegistry,
    integration_connections: dict[str, dict[str, object]],
    write_sql_connections: Callable[[dict[str, dict[str, object]]], Path],
) -> None:
    native = _configure_nested(integration_connections, write_sql_connections)
    alias = "ch_target"
    setting = sql.read(
        alias,
        "SELECT getSetting('flatten_nested')",
        retry_cnt=1,
        query_label=_label("ch-storage-alias-setting"),
    )
    assert int(setting.iloc[0, 0]) == 0
    table, name = _owned_table(alias, resource_registry)
    metadata = _metadata(alias, name)
    assert list(metadata) == list(_SCHEMA)
    assert metadata["aggregate_value"] == "SimpleAggregateFunction(sum, UInt64)"
    assert metadata["point_value"] in {"Point", "Tuple(Float64, Float64)"}
    assert metadata["ring_value"] in {"Ring", "Array(Point)", "Array(Tuple(Float64, Float64))"}
    assert metadata["events"].startswith(("Nested(", "Array(Tuple("))
    expected = [
        (1, 7, 1.25, 2.5, [1.25, 3.5, 1.25], [2.5, 4.75, 2.5], [7, _UINT64_MAX], ["a", "b"]),
        (2, _UINT64_MAX, -3.5, 4.75, [], [], [], []),
    ]
    for explicit in (True, False):
        incoming, original = _frame(native=native), _frame(native=native)
        assert _load(alias, table, incoming, explicit=explicit) == 2
        pd.testing.assert_frame_equal(incoming, original)
        assert _rows(alias, table) == expected
        assert _metadata(alias, name) == metadata

        invalid = _frame(native=native)
        invalid.loc[1, "aggregate_value"] = -1
        with pytest.raises(ValueError, match=r"ClickHouse|authoritative|[Ii]nvalid|[Ee]xceed"):
            _load(alias, table, invalid, explicit=explicit)
        assert _rows(alias, table) == expected
