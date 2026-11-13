"""Greenplum required/default/automatic column metadata remains authoritative."""

import pytest
from analytics_toolkit.sql.backends import get_backend_adapter

from tests.sql._support.column_constraints import metadata_connection


@pytest.mark.parametrize("has_default", [False, True])
def test_gp_explicit_null_never_invokes_a_required_column_default(has_default):
    connection, statements, closed = metadata_connection([("required", True, has_default, "", "")])
    with pytest.raises(ValueError, match=r"authoritative NOT NULL.*required"):
        get_backend_adapter("gp").validate_existing_dataframe_insert_columns(
            connection,
            '"schema.with.dot"."events"',
            ["required"],
            connection_key="gp",
            null_columns=["required"],
            row_count=1,
        )
    assert statements[0][1] == ("schema.with.dot", "events")
    assert closed == [True]


@pytest.mark.parametrize(
    ("metadata", "allowed"),
    [
        ((True, False, "", ""), False),
        ((True, True, "", ""), True),
        ((True, False, "s", ""), True),
        ((True, False, "", "a"), True),
        ((True, False, "", "d"), True),
        ((False, False, "s", ""), True),
    ],
)
def test_gp_omitted_required_columns_need_reported_defaults_or_generation(metadata, allowed):
    connection, _statements, closed = metadata_connection([("required", *metadata)])
    adapter = get_backend_adapter("gp")
    if allowed:
        adapter.validate_existing_dataframe_insert_columns(
            connection, "public.events", ["id"], connection_key="gp", row_count=1
        )
    else:
        with pytest.raises(ValueError, match=r"omits required NOT NULL.*required"):
            adapter.validate_existing_dataframe_insert_columns(
                connection, "public.events", ["id"], connection_key="gp", row_count=1
            )
    assert closed == [True]


def test_gp_nullable_nulls_and_zero_row_required_omissions_remain_valid():
    adapter = get_backend_adapter("gp")
    connection, _statements, _closed = metadata_connection([])
    adapter.validate_existing_dataframe_insert_columns(
        connection, "public.events", ["nullable"], connection_key="gp", null_columns=["nullable"]
    )
    connection, _statements, _closed = metadata_connection([("required", True, False, "", "")])
    adapter.validate_existing_dataframe_insert_columns(
        connection, "public.events", [], connection_key="gp", row_count=0
    )


@pytest.mark.parametrize(
    "row",
    [
        ("required",),
        ("required", None, False, "", ""),
        ("required", True, None, "", ""),
        ("required", True, False, None, ""),
        ("required", True, False, "", None),
    ],
)
def test_gp_incomplete_required_metadata_fails_before_a_write(row):
    connection, _statements, closed = metadata_connection([row])
    with pytest.raises(ValueError, match=r"Cannot verify.*constraints"):
        get_backend_adapter("gp").validate_existing_dataframe_insert_columns(
            connection, "public.events", ["required"], connection_key="gp"
        )
    assert closed == [True]


def test_gp_required_default_snapshot_preserves_identity_and_automatic_values():
    rows = [
        ("Required", True, False, "", ""),
        (" required ", True, True, "", ""),
        ("computed", True, False, "s", ""),
        ("identity_always", True, False, "", "a"),
        ("identity_default", True, False, "", "d"),
        ("nullable_generated", False, False, "s", ""),
    ]
    connection, statements, closed = metadata_connection(rows)
    snapshot = get_backend_adapter("gp").get_required_column_defaults(
        connection, '"schema.with.dot"." spaced.table "', connection_key="gp"
    )
    assert list(snapshot.items()) == [
        ("Required", False),
        (" required ", True),
        ("computed", True),
        ("identity_always", True),
        ("identity_default", True),
    ]
    assert len(statements) == 1
    assert statements[0][1] == ("schema.with.dot", " spaced.table ")
    assert "row_to_json(a)->>'attgenerated'" in statements[0][0]
    assert "row_to_json(a)->>'attidentity'" in statements[0][0]
    assert closed == [True]


def test_gp_dataframe_constraints_reuse_one_metadata_snapshot():
    connection, statements, closed = metadata_connection([("required", True, True, "", "")])
    assert (
        get_backend_adapter("gp").validate_existing_dataframe_insert_columns(
            connection, "public.events", ["id"], connection_key="gp", row_count=1
        )
        is None
    )
    assert len(statements) == 1
    assert closed == [True]


def test_gp_required_default_snapshot_preserves_failure_and_cursor_cleanup():
    connection, statements, closed = metadata_connection(
        [], failure=ConnectionError("catalog unavailable")
    )
    with pytest.raises(ConnectionError, match="catalog unavailable"):
        get_backend_adapter("gp").get_required_column_defaults(
            connection, "public.events", connection_key="gp"
        )
    assert len(statements) == 1
    assert closed == [True]
