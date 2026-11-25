"""Stored native checkpoints bind the actual GP session assignment settings."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace

import pytest
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.gp.native_assignment import (
    NativeAssignmentContext,
    NativeAssignmentError,
    capture_native_assignment_context,
    certify_assignment_checkpoint,
    require_native_assignment_context,
)


class ContextCursor:
    def __init__(self, connection):
        self.connection = connection
        self.closed = False

    def execute(self, statement):
        self.connection.queries.append(statement)
        if self.connection.failure is not None:
            raise self.connection.failure

    def fetchall(self):
        return self.connection.rows

    def fetchone(self):
        return self.connection.stored_counts

    def close(self):
        self.closed = True


class ContextConnection:
    def __init__(self, rows, *, failure=None):
        self.rows = rows
        self.failure = failure
        self.queries = []
        self.cursors = []
        self.stored_counts = (2, 1)

    def cursor(self):
        cursor = ContextCursor(self)
        self.cursors.append(cursor)
        return cursor


def test_native_assignment_context_preserves_actual_settings_and_is_immutable():
    row = ("Europe/Moscow", "SQL, DMY", "", "sql_standard")
    connection = ContextConnection([row])
    context = capture_native_assignment_context(
        get_backend_adapter("gp"), connection, query_label="assignment-context"
    )
    assert context == NativeAssignmentContext(*row)
    assert len(connection.queries) == 1
    assert "assignment-context" in connection.queries[0]
    assert "SET " not in connection.queries[0].upper()
    assert connection.cursors[0].closed
    with pytest.raises(FrozenInstanceError):
        context.timezone = "UTC"


@pytest.mark.parametrize(
    "field", ["timezone", "datestyle", "timezone_abbreviations", "intervalstyle"]
)
def test_native_assignment_context_rejects_each_changed_setting_without_echo(field):
    expected = NativeAssignmentContext("UTC", "ISO, MDY", "Default", "postgres")
    actual = replace(expected, **{field: "private-session-setting"})
    connection = ContextConnection(
        [(actual.timezone, actual.datestyle, actual.timezone_abbreviations, actual.intervalstyle)]
    )
    with pytest.raises(NativeAssignmentError, match="session context differs") as error:
        require_native_assignment_context(get_backend_adapter("gp"), connection, expected)
    assert error.value.analytics_toolkit_sql_retry_safe is False
    assert "private-session-setting" not in str(error.value)
    assert connection.cursors[0].closed


@pytest.mark.parametrize(
    "rows", [[], [("UTC",)], [("UTC", "ISO", "Default", None)], [("a",) * 4] * 2]
)
def test_native_assignment_context_requires_one_complete_settings_row(rows):
    connection = ContextConnection(rows)
    with pytest.raises(NativeAssignmentError, match="session context"):
        capture_native_assignment_context(get_backend_adapter("gp"), connection)
    assert connection.cursors[0].closed


def test_native_assignment_context_closes_failed_cursor_and_sanitizes_driver_values():
    connection = ContextConnection([], failure=ValueError("private-server-value"))
    with pytest.raises(NativeAssignmentError) as error:
        capture_native_assignment_context(get_backend_adapter("gp"), connection)
    assert "private-server-value" not in str(error.value)
    assert error.value.__cause__ is None
    assert connection.cursors[0].closed


def test_native_assignment_context_accepts_identical_actual_settings():
    context = NativeAssignmentContext("UTC", "ISO, MDY", "Default", "postgres")
    connection = ContextConnection([("UTC", "ISO, MDY", "Default", "postgres")])
    require_native_assignment_context(get_backend_adapter("gp"), connection, context)
    assert len(connection.queries) == 1
    assert connection.cursors[0].closed


def test_native_assignment_certificate_binds_session_policy_and_stored_null_counts():
    context = NativeAssignmentContext("UTC", "ISO, MDY", "Default", "postgres")
    connection = ContextConnection([("UTC", "ISO, MDY", "Default", "postgres")])
    certificate = certify_assignment_checkpoint(
        get_backend_adapter("gp"),
        connection,
        "scratch.certified_payload",
        {"payload": "TIMESTAMP"},
        expected_rows=2,
        session_context=context,
    )
    assert certificate.session_context is context
    assert certificate.row_count == 2
    assert certificate.null_columns == ("payload",)
    assert len(connection.queries) == 2
    assert "current_setting" in connection.queries[0]
    assert "COUNT" in connection.queries[1]
    assert all(cursor.closed for cursor in connection.cursors)


def test_native_assignment_context_mismatch_precedes_stored_checkpoint_validation():
    expected = NativeAssignmentContext("UTC", "ISO, MDY", "Default", "postgres")
    connection = ContextConnection([("Europe/Moscow", "ISO, MDY", "Default", "postgres")])
    with pytest.raises(NativeAssignmentError, match="session context differs"):
        certify_assignment_checkpoint(
            get_backend_adapter("gp"),
            connection,
            "scratch.certified_payload",
            {"payload": "TIMESTAMP"},
            expected_rows=2,
            session_context=expected,
        )
    assert len(connection.queries) == 1
    assert connection.cursors[0].closed
