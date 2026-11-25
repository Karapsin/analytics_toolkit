"""Adaptive public transfers keep Python calendar scalars through real replay."""

from __future__ import annotations

import datetime as dt

import pytest

from tests.sql._support.adaptive_transfer import install, track_spools
from tests.sql._support.pandas_calendar_boundary import restrict_exact_timestamp_to_nanoseconds


@pytest.mark.parametrize(("source", "target"), [("gp", "trino"), ("trino", "gp")])
@pytest.mark.parametrize("year", [1500, 2299])
def test_public_adaptive_transfer_keeps_wide_python_datetimes_with_nanosecond_pandas_bounds(
    monkeypatch, source, target, year
):
    restrict_exact_timestamp_to_nanoseconds(monkeypatch)
    value = dt.datetime.fromisoformat(f"{year:04d}-06-07T12:34:56.123456")
    scenario = install(
        monkeypatch,
        native_type="TIMESTAMP(6)",
        rows=[(None,), (value,)],
        from_db_key=source,
        from_db_backend=source,
        to_db_key=target,
        to_db_backend=target,
    )
    paths = track_spools(monkeypatch)

    assert scenario.run() == 2

    assert paths
    assert scenario.loaded == [[(None,), (value,)]]
    assert scenario.read_count == 1
    assert scenario.events.index("complete:SELECT id FROM source") < scenario.events.index(
        "create_target"
    )
    assert all(not path.exists() for path in paths)
