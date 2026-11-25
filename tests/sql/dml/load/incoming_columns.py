"""Invalid dataframe write columns cannot clear an existing destination."""

from __future__ import annotations

from copy import deepcopy

import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.dml.load.load_sql_table import AmbiguousTableLoadError

from tests.sql._support.load_column_state import existing_column_target


def _load(backend, frame, **options):
    return sql.load_df(
        backend,
        "sandbox.events",
        frame,
        retry_cnt=1,
        timeout_increment=0,
        **options,
    )


def _assert_invalid_load_preserves_target(backend, frame, store, **options):
    original_rows = deepcopy(store.rows)
    original_frame = frame.copy(deep=True)
    failure = None
    try:
        _load(backend, frame, write_mode="truncate_insert", **options)
    except (ValueError, KeyError, AttributeError, TypeError, AmbiguousTableLoadError) as error:
        failure = error
    assert store.rows == original_rows
    assert store.submissions == []
    pd.testing.assert_frame_equal(frame, original_frame)
    assert isinstance(failure, ValueError)
    return failure


@pytest.mark.parametrize("backend", ["gp", "trino", "ch"])
@pytest.mark.parametrize("explicit_schema", [False, True])
def test_unknown_existing_target_columns_fail_before_destructive_load(
    monkeypatch, backend, explicit_schema
):
    store = existing_column_target(monkeypatch, backend)
    frame = pd.DataFrame({"id": [7], "extra": [9]})
    options = (
        {"table_schema": {"id": store.schema["id"], "extra": store.schema["id"]}}
        if explicit_schema
        else {}
    )

    failure = _assert_invalid_load_preserves_target(backend, frame, store, **options)

    assert "extra" in str(failure)


@pytest.mark.parametrize("backend", ["gp", "trino", "ch"])
@pytest.mark.parametrize("names", [["id", "id"], [1, "1"]], ids=["same-label", "same-sql-name"])
def test_duplicate_sql_column_names_fail_before_destructive_load(monkeypatch, backend, names):
    store = existing_column_target(monkeypatch, backend, columns=(str(names[0]),))
    frame = pd.DataFrame([[7, 8]], columns=names)

    failure = _assert_invalid_load_preserves_target(backend, frame, store)

    assert "duplicate" in str(failure).lower()


@pytest.mark.parametrize("backend", ["gp", "trino", "ch"])
def test_single_nonstring_label_is_copied_to_its_sql_name_without_mutating_input(
    monkeypatch, backend
):
    store = existing_column_target(monkeypatch, backend, columns=("1",))
    frame = pd.DataFrame([[7]], columns=[1])
    original = frame.copy(deep=True)

    assert _load(backend, frame, write_mode="truncate_insert") == 1

    assert store.rows == [{"1": 7}]
    assert [kind for kind, _detail in store.submissions] == ["CLEAR", "INSERT"]
    pd.testing.assert_frame_equal(frame, original)


@pytest.mark.parametrize("backend", ["gp", "trino", "ch"])
def test_quoted_write_column_spacing_is_preserved(monkeypatch, backend):
    store = existing_column_target(monkeypatch, backend, columns=(" id ",))
    frame = pd.DataFrame({" id ": [7]})
    original = frame.copy(deep=True)

    assert _load(backend, frame, write_mode="truncate_insert") == 1

    assert store.rows == [{" id ": 7}]
    assert [kind for kind, _detail in store.submissions] == ["CLEAR", "INSERT"]
    pd.testing.assert_frame_equal(frame, original)


@pytest.mark.parametrize("backend", ["gp", "trino", "ch"])
@pytest.mark.parametrize("subset", [False, True], ids=["reordered", "subset"])
def test_known_subset_and_reordered_append_preserve_existing_rows(monkeypatch, backend, subset):
    store = existing_column_target(monkeypatch, backend, columns=("id", "value"))
    original_rows = deepcopy(store.rows)
    frame = pd.DataFrame({"id": [7]}) if subset else pd.DataFrame({"value": [9], "id": [7]})
    original_frame = frame.copy(deep=True)

    assert _load(backend, frame, write_mode="append") == 1

    assert store.rows == [*original_rows, {"id": 7, "value": None if subset else 9}]
    assert [kind for kind, _detail in store.submissions] == ["INSERT"]
    pd.testing.assert_frame_equal(frame, original_frame)


@pytest.mark.parametrize("backend", ["gp", "trino", "ch"])
def test_complete_truncate_load_replaces_only_known_target_rows(monkeypatch, backend):
    store = existing_column_target(monkeypatch, backend, columns=("id", "value"))
    frame = pd.DataFrame({"value": [9], "id": [7]})

    assert _load(backend, frame, write_mode="truncate_insert") == 1

    assert store.rows == [{"id": 7, "value": 9}]
    assert [kind for kind, _detail in store.submissions] == ["CLEAR", "INSERT"]
