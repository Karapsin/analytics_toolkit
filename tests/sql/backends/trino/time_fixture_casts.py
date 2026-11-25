"""The fake SQL engine must distinguish emitted casts from target assignment."""

from __future__ import annotations

import datetime as dt

from tests.sql._support.trino_time_values import NativeTimeConnection, NativeTimeDatabase


def test_time_fixture_applies_emitted_cast_before_target_assignment():
    database = NativeTimeDatabase("TIME(6)")
    connection = NativeTimeConnection(database)
    cursor = connection.cursor()
    try:
        cursor.execute(
            'INSERT INTO iceberg.sandbox.target ("payload") VALUES (CAST(? AS TIME(0)))',
            ["12:30:45.600000"],
        )
    finally:
        cursor.close()

    assert database.rows[-1] == dt.time(12, 30, 46)


def test_time_fixture_applies_select_projection_before_target_assignment():
    database = NativeTimeDatabase("TIME(6)")
    source = ("iceberg", "sandbox", "source")
    database.table_rows[source] = [dt.time(12, 30, 45, 600000)]
    database.table_types[source] = "TIME(6)"
    connection = NativeTimeConnection(database)
    cursor = connection.cursor()
    try:
        cursor.execute(
            'INSERT INTO iceberg.sandbox.target ("payload") '
            'SELECT CAST("payload" AS TIME(0)) FROM iceberg.sandbox.source'
        )
    finally:
        cursor.close()

    assert database.rows[-1] == dt.time(12, 30, 46)
