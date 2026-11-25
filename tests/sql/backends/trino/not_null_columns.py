"""Trino uses connector-reported nullability and defaults before clearing rows."""

import pytest
from analytics_toolkit.sql.backends import get_backend_adapter
from sqlglot import exp, parse_one

from tests.sql._support.column_constraints import metadata_connection


@pytest.mark.parametrize("reported_default", [None, "17"])
def test_trino_explicit_null_rejects_reported_not_null_even_with_default(reported_default):
    connection, statements, closed = metadata_connection([("required", "NO", reported_default)])
    with pytest.raises(ValueError, match=r"authoritative NOT NULL.*required"):
        get_backend_adapter("trino").validate_existing_dataframe_insert_columns(
            connection,
            '"catalog.with.dot"." spaced.schema "."events.with.dot"',
            ["required"],
            connection_key="trino",
            null_columns=["required"],
            row_count=1,
        )
    statement, params = statements[0]
    table = parse_one(statement, read="trino").find(exp.Table)
    assert table.catalog == "catalog.with.dot"
    assert table.db == "information_schema"
    assert table.name == "columns"
    assert params == (" spaced.schema ", "events.with.dot")
    assert closed == [True]


@pytest.mark.parametrize("reported_default", [None, "17"])
def test_trino_omissions_require_a_genuine_reported_default(reported_default):
    connection, _statements, closed = metadata_connection([("required", "NO", reported_default)])
    adapter = get_backend_adapter("trino")
    if reported_default is None:
        with pytest.raises(ValueError, match=r"omits required NOT NULL.*required"):
            adapter.validate_existing_dataframe_insert_columns(
                connection, "iceberg.sandbox.events", ["id"], connection_key="trino", row_count=1
            )
    else:
        # This is metadata from a connector reporting a real default, not an
        # assertion that the Iceberg connector supports DEFAULT expressions.
        adapter.validate_existing_dataframe_insert_columns(
            connection, "catalog.sandbox.events", ["id"], connection_key="trino", row_count=1
        )
    assert closed == [True]


def test_trino_nullable_nulls_and_zero_row_required_omissions_remain_valid():
    adapter = get_backend_adapter("trino")
    connection, _statements, _closed = metadata_connection([])
    adapter.validate_existing_dataframe_insert_columns(
        connection,
        "iceberg.sandbox.events",
        ["nullable"],
        connection_key="trino",
        null_columns=["nullable"],
    )
    connection, _statements, _closed = metadata_connection([("required", "NO", None)])
    adapter.validate_existing_dataframe_insert_columns(
        connection, "iceberg.sandbox.events", [], connection_key="trino", row_count=0
    )


@pytest.mark.parametrize("rows", [[("required",)], [("required", "YES", None)]])
def test_trino_incomplete_not_null_metadata_fails_before_a_write(rows):
    connection, _statements, closed = metadata_connection(rows)
    with pytest.raises(ValueError, match=r"Cannot verify.*NOT NULL constraints"):
        get_backend_adapter("trino").validate_existing_dataframe_insert_columns(
            connection, "iceberg.sandbox.events", ["required"], connection_key="trino"
        )
    assert closed == [True]


def test_trino_constraint_lookup_preserves_catalog_failure_and_closes_cursor():
    connection, _statements, closed = metadata_connection(
        [], failure=ConnectionError("catalog unavailable")
    )
    with pytest.raises(ConnectionError, match="catalog unavailable"):
        get_backend_adapter("trino").validate_existing_dataframe_insert_columns(
            connection, "iceberg.sandbox.events", ["id"], connection_key="trino"
        )
    assert closed == [True]


def test_trino_required_default_snapshot_preserves_exact_names_and_namespace():
    connection, statements, closed = metadata_connection(
        [("Required", "NO", None), (" required ", "NO", "17")]
    )
    snapshot = get_backend_adapter("trino").get_required_column_defaults(
        connection,
        '"catalog.with.dot"." spaced.schema "."events.with.dot"',
        connection_key="trino",
    )
    assert list(snapshot.items()) == [("Required", False), (" required ", True)]
    assert len(statements) == 1
    statement, params = statements[0]
    table = parse_one(statement, read="trino").find(exp.Table)
    assert (table.catalog, table.db, table.name) == (
        "catalog.with.dot",
        "information_schema",
        "columns",
    )
    assert params == (" spaced.schema ", "events.with.dot")
    assert closed == [True]


def test_trino_dataframe_constraints_reuse_one_metadata_snapshot():
    connection, statements, closed = metadata_connection([("required", "NO", None)])
    assert (
        get_backend_adapter("trino").validate_existing_dataframe_insert_columns(
            connection,
            "iceberg.sandbox.events",
            ["required"],
            connection_key="trino",
            row_count=1,
        )
        is None
    )
    assert len(statements) == 1
    assert closed == [True]
