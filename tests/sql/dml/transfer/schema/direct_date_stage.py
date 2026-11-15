"""Explicit Trino DATE stages validate converted values and retain raw replay input."""

from __future__ import annotations

import datetime as dt

import pytest
from analytics_toolkit.sql.backends.trino.date_values import TrinoDateValueError
from analytics_toolkit.sql.core.replay_spool import ReplaySpool

from tests.sql._support.adaptive_transfer import track_spools
from tests.sql.dml.transfer.schema.date_authority import _SENTINELS, date_target_transfer


def capture_raw_spool_rows(monkeypatch):
    captured = []
    original_write = ReplaySpool.write_rows

    def write(spool, rows):
        batch = [tuple(row) for row in rows]
        captured.extend(batch)
        return original_write(spool, batch)

    monkeypatch.setattr(ReplaySpool, "write_rows", write)
    return captured


@pytest.mark.parametrize(
    ("native_type", "values", "expected"),
    [
        pytest.param(
            "VARCHAR",
            ["0001-01-01", "2024-02-29", "9999-12-31", None],
            [dt.date(1, 1, 1), dt.date(2024, 2, 29), dt.date(9999, 12, 31), None],
            id="canonical-text-and-null",
        ),
        pytest.param(
            "TIMESTAMP(6) WITH TIME ZONE",
            [
                dt.datetime.fromisoformat("2024-01-01T00:30:00+05:30"),
                dt.datetime.fromisoformat("2024-01-01T23:30:00-07:00"),
                None,
            ],
            [dt.date(2024, 1, 1), dt.date(2024, 1, 1), None],
            id="aware-local-calendar",
        ),
        pytest.param(
            "TIMESTAMP(6)",
            [dt.datetime.fromisoformat("2024-02-29T18:42:43.123456"), None],
            [dt.date(2024, 2, 29), None],
            id="naive-local-calendar",
        ),
    ],
)
def test_direct_date_stage_converts_trino_values_after_one_raw_frozen_scan(
    monkeypatch, native_type, values, expected
):
    raw_rows = [(value,) for value in values]
    captured = capture_raw_spool_rows(monkeypatch)
    scenario, destination = date_target_transfer(monkeypatch, "trino", native_type, values, "DATE")
    paths = track_spools(monkeypatch)

    result = scenario.run(metadata=True)

    assert result.rows == len(values)
    assert result.metadata.input_count == len(values)
    assert scenario.loaded == [[(value,) for value in expected]]
    assert destination.rows == [(value,) for value in expected]
    assert scenario.schemas == [{"id": "DATE"}]
    assert scenario.existing_types == {"id": "DATE"}
    assert captured == raw_rows
    assert [(value,) for value in values] == raw_rows
    assert scenario.read_count == 1
    assert scenario.finalizations == 1
    assert scenario.events.index("complete:SELECT id FROM source") < scenario.events.index(
        "create_target"
    )
    assert paths
    assert all(not path.exists() for path in paths)


@pytest.mark.parametrize("invalid_text", ["not-a-date", "2023-02-29", "01/02/2024"])
def test_direct_date_stage_rejects_late_invalid_trino_text_before_stage_or_target_mutation(
    monkeypatch, invalid_text
):
    values = ["2024-01-01", None, invalid_text]
    raw_rows = [(value,) for value in values]
    captured = capture_raw_spool_rows(monkeypatch)
    scenario, destination = date_target_transfer(monkeypatch, "trino", "VARCHAR", values, "DATE")
    paths = track_spools(monkeypatch)

    with pytest.raises(TrinoDateValueError, match="DATE") as failure:
        scenario.run()

    message = str(failure.value)
    assert "CAST" in message
    assert "from_sql" in message
    assert invalid_text not in message
    assert "2024-01-01" not in message
    assert destination.rows == _SENTINELS
    assert destination.statements == []
    assert scenario.existing_types == {"id": "DATE"}
    assert "create_target" not in scenario.events
    assert not scenario.loaded
    assert scenario.finalizations == 0
    assert scenario.read_count == 1
    assert len(captured) >= 2
    assert captured == raw_rows[: len(captured)]
    assert paths
    assert all(not path.exists() for path in paths)
