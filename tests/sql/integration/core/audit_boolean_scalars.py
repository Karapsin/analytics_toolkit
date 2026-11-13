"""Disposable native integer and Boolean transport checks for NumPy bool cells."""

from __future__ import annotations

import os
import uuid
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd
import pytest
from analytics_toolkit import sql

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


def _schema(backend: str) -> dict[str, str]:
    if backend == "ch":
        return {
            "row_id": "Int64",
            "as_integer": "Nullable(Int64)",
            "as_boolean": "Nullable(Bool)",
            "integer_items": "Array(Nullable(Int64))",
            "boolean_items": "Array(Nullable(Bool))",
        }
    return {
        "row_id": "BIGINT",
        "as_integer": "BIGINT",
        "as_boolean": "BOOLEAN",
        "integer_items": "BIGINT[]" if backend == "gp" else "ARRAY(BIGINT)",
        "boolean_items": "BOOLEAN[]" if backend == "gp" else "ARRAY(BOOLEAN)",
    }


def _options(backend: str) -> dict[str, Any]:
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


def _object_booleans() -> pd.Series:
    return pd.Series([np.bool_(False), np.bool_(True), None, np.bool_(False)], dtype=object)


def _nested_booleans() -> pd.Series:
    return pd.Series(
        [[np.bool_(False), np.bool_(True), None], [np.bool_(True)], [None], []], dtype=object
    )


@pytest.mark.parametrize(
    "backend",
    [scenario_param(f"audit.load.numpy_booleans.{backend}", backend) for backend in BACKENDS],
)
def test_object_numpy_booleans_load_as_native_zero_one_or_boolean_values(
    backend: str,
    resource_registry: ResourceRegistry,
) -> None:
    if not backend_enabled(backend):
        pytest.skip("Greenplum requires x86_64")
    alias = backend_alias(backend, target=True)
    table = resource_registry.table(
        alias,
        integration_table(backend, "numpy_boolean"),
        ch_cluster="integration_cluster" if backend == "ch" else None,
    )
    options = _options(backend)
    seed = pd.DataFrame(
        {
            "row_id": [99],
            "as_integer": [9],
            "as_boolean": [True],
            "integer_items": [[9]],
            "boolean_items": [[True]],
        }
    )
    assert (
        sql.load_df(
            alias,
            table,
            seed,
            write_mode="replace",
            table_schema=_schema(backend),
            retry_cnt=1,
            query_label=_label("numpy-boolean-seed"),
            **options,
        )
        == 1
    )
    incoming = pd.DataFrame(
        {
            "row_id": [1, 2, 3, 4],
            "as_integer": _object_booleans(),
            "as_boolean": _object_booleans(),
            "integer_items": _nested_booleans(),
            "boolean_items": _nested_booleans(),
        }
    )
    assert incoming["as_integer"].dtype == object
    assert type(incoming.iloc[0]["as_integer"]) is np.bool_
    assert type(incoming.iloc[1]["as_integer"]) is np.bool_
    assert (
        sql.load_df(
            alias,
            table,
            incoming,
            write_mode="truncate_insert",
            retry_cnt=1,
            query_label=_label("numpy-boolean-load"),
            **options,
        )
        == 4
    )
    assert type(incoming.iloc[0]["as_integer"]) is np.bool_
    assert type(incoming.iloc[0]["as_boolean"]) is np.bool_
    assert type(incoming.iloc[0]["integer_items"][0]) is np.bool_

    actual = sql.read(
        alias,
        f"SELECT row_id, as_integer, as_boolean, integer_items, boolean_items "
        f"FROM {table} ORDER BY row_id",
        retry_cnt=1,
        query_label=_label("numpy-boolean-check"),
    )
    assert actual["row_id"].tolist() == [1, 2, 3, 4]
    for index, expected in ((0, 0), (1, 1), (3, 0)):
        value = actual.iloc[index]["as_integer"]
        assert isinstance(value, (int, np.integer))
        assert not isinstance(value, (bool, np.bool_))
        assert value == expected
    assert pd.isna(actual.iloc[2]["as_integer"])
    for index, expected in ((0, False), (1, True), (3, False)):
        value = actual.iloc[index]["as_boolean"]
        assert not pd.isna(value)
        assert bool(value) is expected
    assert pd.isna(actual.iloc[2]["as_boolean"])
    integer_items = [list(value) for value in actual["integer_items"]]
    assert integer_items == [[0, 1, None], [1], [None], []]
    for values in integer_items:
        for value in values:
            if value is not None:
                assert isinstance(value, (int, np.integer))
                assert not isinstance(value, (bool, np.bool_))
    assert [list(value) for value in actual["boolean_items"]] == [
        [False, True, None],
        [True],
        [None],
        [],
    ]
