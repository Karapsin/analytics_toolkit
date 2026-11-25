"""Native clock declarations retain their family across adaptive inference."""

from __future__ import annotations

import datetime as dt

import pytest

from tests.sql._support.adaptive_transfer import install


@pytest.mark.parametrize("empty", [False, True], ids=["all-null", "empty"])
@pytest.mark.parametrize("aware", [False, True])
def test_public_transfer_retains_native_time_family_for_no_observed_clock(
    monkeypatch, empty, aware
):
    native = "TIME(6)" + (" WITH TIME ZONE" if aware else "")
    scenario = install(
        monkeypatch,
        native_type=native,
        rows=[] if empty else [(None,), (None,)],
        from_db_key="gp",
        from_db_backend="gp",
        to_db_key="trino",
        to_db_backend="trino",
    )
    if empty:
        # The scenario factory supplies default rows for a falsey input list.
        scenario.batches[scenario.options.source_sql] = []

    result = scenario.run()

    assert result == (0 if empty else 2)
    assert scenario.schemas == [{"id": native}]
    assert scenario.loaded == ([[]] if empty else [[(None,), (None,)]])
    assert scenario.read_count == 1


@pytest.mark.parametrize("aware", [False, True])
def test_public_transfer_infers_native_time_from_complete_clock_values(monkeypatch, aware):
    native = "TIME(6)" + (" WITH TIME ZONE" if aware else "")
    zone = dt.timezone(dt.timedelta(hours=3)) if aware else None
    clock = dt.time(12, 34, 56, 123456, tzinfo=zone)
    scenario = install(
        monkeypatch,
        native_type=native,
        rows=[(None,), (clock,)],
        from_db_key="gp",
        from_db_backend="gp",
        to_db_key="trino",
        to_db_backend="trino",
    )

    assert scenario.run() == 2

    assert scenario.schemas == [{"id": native}]
    assert scenario.loaded == [[(None,), (clock,)]]
    assert scenario.read_count == 1
