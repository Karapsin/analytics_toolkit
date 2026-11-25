"""NOT NULL constraints are checked before destructive dataframe loading."""

from __future__ import annotations

from copy import deepcopy
from enum import Enum

import numpy as np
import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.dml.load.load_sql_table import AmbiguousTableLoadError

from tests.sql._support.load_column_state import existing_column_target


class NullValue(Enum):
    EMPTY = None


def _load(backend, frame, **options):
    return sql.load_df(
        backend,
        "sandbox.events",
        frame,
        write_mode=options.pop("write_mode", "truncate_insert"),
        retry_cnt=1,
        timeout_increment=0,
        **options,
    )


@pytest.mark.parametrize("backend", ["gp", "trino"])
@pytest.mark.parametrize(
    "value", [None, pd.NA, np.nan, NullValue.EMPTY], ids=["none", "pdNA", "nan", "enum"]
)
@pytest.mark.parametrize("explicit_schema", [False, True], ids=["live-schema", "explicit-schema"])
def test_existing_not_null_column_rejects_null_before_clearing_seed_rows(
    monkeypatch, backend, value, explicit_schema
):
    store = existing_column_target(
        monkeypatch,
        backend,
        real_metadata=True,
        real_gp_adaptation=backend == "gp",
        not_null=("id",),
    )
    store.schema["id"] = "BIGINT"
    original_rows = deepcopy(store.rows)
    frame = pd.DataFrame({"id": pd.Series([7, value], dtype=object)})
    original_frame = frame.copy(deep=True)
    failure = None
    try:
        _load(backend, frame, **({"table_schema": {"id": "BIGINT"}} if explicit_schema else {}))
    except (ValueError, AmbiguousTableLoadError) as error:
        failure = error

    assert store.rows == original_rows
    assert store.submissions == []
    assert isinstance(failure, ValueError)
    pd.testing.assert_frame_equal(frame, original_frame)


@pytest.mark.parametrize("backend", ["gp", "trino"])
def test_omitted_required_not_null_column_rejects_before_clearing_seed_rows(monkeypatch, backend):
    store = existing_column_target(
        monkeypatch,
        backend,
        columns=("id", "required"),
        real_metadata=True,
        real_gp_adaptation=backend == "gp",
        not_null=("required",),
    )
    original_rows = deepcopy(store.rows)
    frame = pd.DataFrame({"id": [7]})
    original_frame = frame.copy(deep=True)

    with pytest.raises(ValueError, match="required"):
        _load(backend, frame)

    assert store.rows == original_rows
    assert store.submissions == []
    pd.testing.assert_frame_equal(frame, original_frame)


@pytest.mark.parametrize("backend", ["gp", "trino"])
def test_complete_upsert_with_null_nonkey_required_value_rejects_before_staging(
    monkeypatch, backend
):
    store = existing_column_target(
        monkeypatch,
        backend,
        columns=("id", "part", "payload"),
        real_metadata=True,
        real_gp_adaptation=backend == "gp",
        not_null=("id", "part", "payload"),
    )
    original_rows = deepcopy(store.rows)
    frame = pd.DataFrame({"id": [7], "part": [1], "payload": pd.Series([None], dtype=object)})
    original_frame = frame.copy(deep=True)

    with pytest.raises(ValueError, match="authoritative NOT NULL"):
        _load(
            backend, frame, write_mode="upsert", key_columns=["id"], upsert_partition_column="part"
        )

    assert store.rows == original_rows
    assert store.submissions == []
    pd.testing.assert_frame_equal(frame, original_frame)


@pytest.mark.parametrize("backend", ["gp", "trino"])
def test_nullable_columns_allow_supplied_and_omitted_sql_null(monkeypatch, backend):
    store = existing_column_target(
        monkeypatch,
        backend,
        columns=("id", "provided", "omitted"),
        real_metadata=True,
        real_gp_adaptation=backend == "gp",
        not_null=("id",),
    )
    frame = pd.DataFrame({"id": [7], "provided": pd.Series([None], dtype=object)})
    original_frame = frame.copy(deep=True)

    assert _load(backend, frame) == 1

    assert store.rows == [{"id": 7, "provided": None, "omitted": None}]
    assert [kind for kind, _detail in store.submissions] == ["CLEAR", "INSERT"]
    pd.testing.assert_frame_equal(frame, original_frame)


def test_greenplum_omitted_not_null_default_is_populated_by_the_target(monkeypatch):
    store = existing_column_target(
        monkeypatch,
        "gp",
        columns=("id", "required"),
        real_metadata=True,
        real_gp_adaptation=True,
        not_null=("required",),
        defaults={"required": 17},
    )

    assert _load("gp", pd.DataFrame({"id": [7]})) == 1

    assert store.rows == [{"id": 7, "required": 17}]
    assert [kind for kind, _detail in store.submissions] == ["CLEAR", "INSERT"]


@pytest.mark.parametrize("kind", ["GENERATED", "IDENTITY_ALWAYS", "IDENTITY_DEFAULT"])
def test_greenplum_omitted_generated_and_identity_not_null_values_are_populated(monkeypatch, kind):
    store = existing_column_target(
        monkeypatch,
        "gp",
        columns=("id", "calc"),
        real_metadata=True,
        real_gp_adaptation=True,
        not_null=("calc",),
        kinds={"calc": kind},
    )

    assert _load("gp", pd.DataFrame({"id": [7]})) == 1

    assert store.rows == [{"id": 7, "calc": 14 if kind == "GENERATED" else 1000}]
    assert [kind for kind, _detail in store.submissions] == ["CLEAR", "INSERT"]


@pytest.mark.parametrize("backend", ["gp", "trino"])
@pytest.mark.parametrize(
    "value", ["NULL", "None", "NaN"], ids=["SQL-word", "None-word", "NaN-word"]
)
def test_null_looking_text_is_not_a_sql_null_column(monkeypatch, backend, value):
    store = existing_column_target(
        monkeypatch,
        backend,
        real_metadata=True,
        real_gp_adaptation=backend == "gp",
        not_null=("id",),
    )
    store.schema["id"] = "TEXT" if backend == "gp" else "VARCHAR"
    frame = pd.DataFrame({"id": [value]})

    assert _load(backend, frame) == 1

    assert store.rows == [{"id": value}]


@pytest.mark.parametrize("backend", ["gp", "trino"])
def test_native_array_with_null_elements_is_not_a_sql_null_column(monkeypatch, backend):
    store = existing_column_target(
        monkeypatch,
        backend,
        real_metadata=True,
        real_gp_adaptation=backend == "gp",
        not_null=("id",),
    )
    store.schema["id"] = "BIGINT[]" if backend == "gp" else "ARRAY(BIGINT)"
    frame = pd.DataFrame({"id": pd.Series([[None, 7]], dtype=object)})
    original_frame = frame.copy(deep=True)

    assert _load(backend, frame) == 1

    assert store.rows == [{"id": [None, 7]}]
    pd.testing.assert_frame_equal(frame, original_frame)


@pytest.mark.parametrize("backend", ["gp", "trino"])
def test_explicit_text_encoding_of_null_container_is_not_a_sql_null_column(monkeypatch, backend):
    store = existing_column_target(
        monkeypatch,
        backend,
        real_metadata=True,
        real_gp_adaptation=backend == "gp",
        not_null=("id",),
    )
    native_type = "TEXT" if backend == "gp" else "VARCHAR"
    store.schema["id"] = native_type
    frame = pd.DataFrame({"id": pd.Series([[None]], dtype=object)})

    assert _load(backend, frame, table_schema={"id": native_type}) == 1

    assert store.rows == [{"id": '{"v":1,"backend":"","type":"","value":["array",[["null"]]]}'}]
