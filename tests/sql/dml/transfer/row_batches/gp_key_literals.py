"""Public keyed transfers preserve aware Greenplum timestamp-key instants."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.dml.transfer.flow import api
from analytics_toolkit.sql.dml.transfer.io.source import iter_source_batches
from analytics_toolkit.sql.dml.transfer.runtime import retry as retry_module
from psycopg2 import extensions

from tests.sql._support.gp_key_selection import TimestampKeyConnection, TimestampKeyCursor


@pytest.fixture(autouse=True)
def _fake_key_source_driver_boundary(monkeypatch):
    register_type = extensions.register_type

    def register(caster, scope=None):
        if isinstance(scope, TimestampKeyCursor):
            scope.__dict__.setdefault("registered_typecasters", []).append(caster)
            return None
        return register_type(caster, scope)

    def factory(*_args, **_kwargs):
        return TimestampKeyConnection(aware_column=True)

    monkeypatch.setattr(extensions, "register_type", register)
    monkeypatch.setattr(get_backend_adapter("gp"), "open_connection", factory)
    monkeypatch.setattr(retry_module, "get_sql_connection", factory)


@pytest.mark.parametrize(
    ("key", "aware_column", "row_id"),
    [
        (
            datetime(2025, 1, 1, 3, 0, 0, 123456, tzinfo=timezone(timedelta(hours=3))),
            True,
            "midnight",
        ),
        (
            datetime(2024, 12, 31, 19, 0, 0, 123456, tzinfo=timezone(timedelta(hours=-5))),
            True,
            "midnight",
        ),
        (datetime(2025, 1, 1, 0, 0, 0, 123456, tzinfo=timezone.utc), True, "midnight"),
        (datetime.fromisoformat("2025-01-01T03:00:00.123456"), False, "three"),
    ],
    ids=["positive-offset", "negative-offset", "utc", "naive"],
)
def test_keyed_transfer_selects_timestamp_row_by_emitted_literal_semantics(
    monkeypatch, key, aware_column, row_id
):
    source = TimestampKeyConnection(aware_column=aware_column)
    transferred = []

    def copy_selected_rows(*, options, read_retry_cnt, **_kwargs):
        assert options.from_db_backend == "gp"
        assert options.transfer_slices is not None
        try:
            for transfer_slice in options.transfer_slices:
                for batch in iter_source_batches(
                    options.from_db_key,
                    options.from_db_backend,
                    {"connection": source},
                    transfer_slice.source_sql,
                    10,
                    read_retry_cnt,
                    0,
                ):
                    transferred.extend(batch.rows)
        finally:
            source.close()
        return len(transferred)

    # Keep public option/key normalization and the real GP batch reader. Only
    # the DB-API server and the unrelated target/staging domain are fake.
    monkeypatch.setattr(api, "run_transfer_attempt", copy_selected_rows)
    copied = sql.transfer(
        "gp",
        "trino",
        from_sql="SELECT stamp, row_id FROM source_table WHERE {stamp}",
        to_table="sandbox.target",
        transfer_keys="stamp",
        transfer_key_values=[key],
        concurrency=1,
        ignore_source_staging=True,
        retry_cnt=1,
        full_retry_cnt=1,
        validate_row_count=False,
    )
    assert copied == 1
    assert [row[1] for row in transferred] == [row_id]
    expected = key.astimezone(timezone.utc) if aware_column else key
    assert transferred[0][0] == expected
    assert (transferred[0][0].tzinfo is not None) is aware_column
    assert source.queries
    assert all(cursor.closed for cursor in source.cursors)
    assert source.closed


def test_timestamp_key_fixture_rejects_timestamp_comparison_with_text_row_id(monkeypatch):
    source = TimestampKeyConnection(aware_column=True)
    monkeypatch.setattr(
        retry_module, "get_sql_connection", lambda _key: TimestampKeyConnection(aware_column=True)
    )

    def copy_selected_rows(*, options, read_retry_cnt, **_kwargs):
        try:
            return sum(
                len(batch.rows)
                for transfer_slice in options.transfer_slices
                for batch in iter_source_batches(
                    options.from_db_key,
                    options.from_db_backend,
                    {"connection": source},
                    transfer_slice.source_sql,
                    10,
                    read_retry_cnt,
                    0,
                )
            )
        finally:
            source.close()

    monkeypatch.setattr(api, "run_transfer_attempt", copy_selected_rows)
    with pytest.raises(ValueError, match=r"(?i)row_id.*timestamp"):
        sql.transfer(
            "gp",
            "trino",
            from_sql="SELECT stamp, row_id FROM source_table WHERE {row_id}",
            to_table="sandbox.target",
            transfer_keys="row_id",
            transfer_key_values=[datetime(2025, 1, 1, 0, 0, 0, 123456, tzinfo=timezone.utc)],
            concurrency=1,
            ignore_source_staging=True,
            retry_cnt=1,
            full_retry_cnt=1,
            validate_row_count=False,
        )
    assert all(cursor.closed for cursor in source.cursors)
    assert source.closed


def test_timestamp_key_fixture_selects_the_actual_text_column():
    source = TimestampKeyConnection(aware_column=True)
    cursor = source.cursor()
    cursor.execute("SELECT stamp, row_id FROM source_table WHERE row_id = 'three'")
    assert cursor.fetchmany(10) == [source.rows[1]]


def test_timestamp_key_fixture_rejects_wrong_left_column_without_driver_registration():
    source = TimestampKeyConnection(aware_column=True)
    cursor = source.cursor()
    with pytest.raises(ValueError, match=r"(?i)row_id.*timestamp"):
        cursor.execute(
            "SELECT stamp, row_id FROM source_table "
            "WHERE row_id = TIMESTAMPTZ '2025-01-01 00:00:00.123456+00:00'"
        )
