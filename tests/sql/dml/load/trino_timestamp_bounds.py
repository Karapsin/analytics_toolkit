"""Wide native timestamp certification precedes live destination mutation."""

from __future__ import annotations

import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends.calendar_ticks import days_from_civil
from analytics_toolkit.sql.core.exact_values import ExactTemporal

from tests.sql._support.load_table import load_df_module
from tests.sql._support.trino_timestamp_bounds import timestamp_bounds_target


@pytest.mark.parametrize(
    ("precision", "aware"), [(0, False), (6, False), (12, False), (0, True), (6, True), (12, True)]
)
def test_public_load_rejects_outside_native_trino_timestamp_coordinates_before_clear(
    monkeypatch, precision, aware
):
    native = f"TIMESTAMP({precision})" + (" WITH TIME ZONE" if aware else "")
    database = timestamp_bounds_target(monkeypatch, native)
    year = 100000 if aware else 1000000
    value = ExactTemporal(
        days_from_civil(year, 1, 1) * 86400 * 10**precision,
        precision,
        "UTC" if aware else None,
        wall_time=not aware,
    )
    frame = pd.DataFrame({"payload": pd.Series([None, value], dtype=object)})

    try:
        with pytest.raises(ValueError, match=r"(?i)timestamp|coordinate|storage"):
            sql.load_df(
                "trino",
                "iceberg.sandbox.target",
                frame,
                write_mode="truncate_insert",
                retry_cnt=1,
                timeout_increment=0,
            )
    finally:
        assert database.rows == ["preserved-original"]
        assert database.mutations == []

    assert frame.iloc[1, 0] is value
    assert all(connection.close_calls == 1 for connection in database.opened)


@pytest.mark.parametrize("aware", [False, True])
def test_public_load_keeps_engine_supported_wide_timestamp_in_verified_memory_connector(
    monkeypatch, aware
):
    native = "TIMESTAMP(12)" + (" WITH TIME ZONE" if aware else "")
    database = timestamp_bounds_target(monkeypatch, native, connector="memory")
    value = ExactTemporal(
        days_from_civil(10000, 1, 1) * 86400 * 10**12 + 1,
        12,
        "UTC" if aware else None,
        wall_time=not aware,
    )
    frame = pd.DataFrame({"payload": pd.Series([value, None], dtype=object)})

    assert (
        sql.load_df(
            "trino",
            "iceberg.sandbox.target",
            frame,
            write_mode="truncate_insert",
            retry_cnt=1,
            timeout_increment=0,
        )
        == 2
    )

    assert database.rows[0].startswith("+10000-01-01")
    assert database.rows[1] is None
    assert frame.iloc[0, 0] is value


def test_public_load_rejects_wide_iceberg_nano_timestamp_before_any_mutation(monkeypatch):
    database = timestamp_bounds_target(
        monkeypatch, "TIMESTAMP(9)", connector="iceberg", format_version=3
    )
    value = ExactTemporal(days_from_civil(10000, 1, 1) * 86400 * 10**9 + 1, 9, wall_time=True)
    frame = pd.DataFrame({"payload": pd.Series([None, value], dtype=object)})

    try:
        with pytest.raises(ValueError, match=r"(?i)timestamp|nanosecond|storage"):
            sql.load_df(
                "trino",
                "iceberg.sandbox.target",
                frame,
                write_mode="truncate_insert",
                retry_cnt=1,
                timeout_increment=0,
            )
    finally:
        assert database.rows == ["preserved-original"]
        assert database.mutations == []


@pytest.mark.parametrize("planning", [{"dry_run": True}, {"return_sql": True}])
def test_public_timestamp_plan_reports_pending_storage_capability_without_opening_connection(
    monkeypatch, planning
):
    def unexpected(_alias):
        message = "Timestamp capability planning must not open a connection."
        raise AssertionError(message)

    monkeypatch.setattr(load_df_module, "get_sql_connection", unexpected)
    value = ExactTemporal(days_from_civil(10000, 1, 1) * 86400 * 10**12 + 1, 12, wall_time=True)
    frame = pd.DataFrame({"payload": pd.Series([value], dtype=object)})
    plan = sql.load_df(
        "trino",
        "iceberg.sandbox.target",
        frame,
        table_schema={"payload": "TIMESTAMP(12)"},
        retry_cnt=1,
        **planning,
    )
    assert plan.options["pending_timestamp_capabilities"]["pending"]
    assert plan.options["pending_timestamp_capabilities"]["columns"] == ["payload"]
    assert frame.iloc[0, 0] is value
