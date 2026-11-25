"""Existing generated columns are checked before destructive dataframe loads."""

from __future__ import annotations

from copy import deepcopy

import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.dml.load.load_sql_table import AmbiguousTableLoadError

from tests.sql._support.load_column_state import existing_column_target


def _load(backend, frame):
    return sql.load_df(
        backend,
        "sandbox.events",
        frame,
        write_mode="truncate_insert",
        retry_cnt=1,
        timeout_increment=0,
    )


@pytest.mark.parametrize(
    ("backend", "kind", "native", "allowed"),
    [
        ("gp", "GENERATED", False, False),
        ("gp", "IDENTITY_ALWAYS", False, False),
        ("ch", "ALIAS", False, False),
        ("ch", "MATERIALIZED", False, False),
        ("ch", "MATERIALIZED", False, True),
        ("ch", "MATERIALIZED", True, False),
    ],
    ids=[
        "gp-generated",
        "gp-identity-always",
        "alias",
        "http-materialized",
        "http-setting",
        "native-default",
    ],
)
def test_noninsertable_existing_columns_reject_before_clearing_seed_rows(
    monkeypatch, backend, kind, native, allowed
):
    store = existing_column_target(
        monkeypatch,
        backend,
        ("id", "calc"),
        real_metadata=True,
        kinds={"calc": kind},
        native=native,
        materialized_allowed=allowed,
    )
    original_rows = deepcopy(store.rows)
    frame = pd.DataFrame({"id": [7], "calc": [99]})
    original_frame = frame.copy(deep=True)
    failure = None
    try:
        _load(backend, frame)
    except (ValueError, AmbiguousTableLoadError) as error:
        failure = error

    assert store.rows == original_rows
    assert store.submissions == []
    assert isinstance(failure, ValueError)
    assert "calc" in str(failure)
    pd.testing.assert_frame_equal(frame, original_frame)


@pytest.mark.parametrize(
    ("backend", "kind", "native", "allowed"),
    [("gp", "IDENTITY_DEFAULT", False, False), ("ch", "MATERIALIZED", True, True)],
    ids=["identity-by-default", "native-materialized-opt-in"],
)
def test_supported_explicit_generated_value_modes_remain_available(
    monkeypatch, backend, kind, native, allowed
):
    store = existing_column_target(
        monkeypatch,
        backend,
        ("id", "calc"),
        real_metadata=True,
        kinds={"calc": kind},
        native=native,
        materialized_allowed=allowed,
    )
    frame = pd.DataFrame({"id": [7], "calc": [99]})
    original = frame.copy(deep=True)

    assert _load(backend, frame) == 1

    assert store.rows == [{"id": 7, "calc": 99}]
    assert [kind for kind, _detail in store.submissions] == ["CLEAR", "INSERT"]
    pd.testing.assert_frame_equal(frame, original)


def test_clickhouse_ephemeral_input_is_accepted_without_becoming_stored_data(monkeypatch):
    store = existing_column_target(
        monkeypatch, "ch", ("id", "ephemeral"), real_metadata=True, kinds={"ephemeral": "EPHEMERAL"}
    )
    frame = pd.DataFrame({"id": [7], "ephemeral": [99]})

    assert _load("ch", frame) == 1

    assert store.rows == [{"id": 7}]
    assert [kind for kind, _detail in store.submissions] == ["CLEAR", "INSERT"]


@pytest.mark.parametrize(("backend", "kind"), [("gp", "GENERATED"), ("ch", "MATERIALIZED")])
def test_omitted_generated_values_are_computed_after_valid_destructive_load(
    monkeypatch, backend, kind
):
    store = existing_column_target(
        monkeypatch, backend, ("id", "calc"), real_metadata=True, kinds={"calc": kind}
    )

    assert _load(backend, pd.DataFrame({"id": [7]})) == 1

    assert store.rows == [{"id": 7, "calc": 14}]
    assert [kind for kind, _detail in store.submissions] == ["CLEAR", "INSERT"]
