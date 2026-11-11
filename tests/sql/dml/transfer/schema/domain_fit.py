from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest
from analytics_toolkit.general import set_connections_path

from tests.sql._support.adaptive_transfer import (
    Scenario,
    install,
    require_streaming_fast_path,
    track_spools,
)


def existing_ch_target(
    monkeypatch: pytest.MonkeyPatch, native: str, target: str, values: list[Any]
) -> Scenario:
    # The autouse SQL fixture owns this temporary .connections file.
    path = Path(".connections")
    configs = json.loads(path.read_text())
    configs["ch_target"] = {**configs["ch"], "database": "target"}
    path.write_text(json.dumps(configs))
    set_connections_path(path)
    scenario = install(
        monkeypatch,
        native_type=native,
        rows=[(value,) for value in values],
        to_db_key="ch_target",
        to_db_backend="ch",
        target_table="target.events",
        write_mode="append",
        replace_target_table=False,
    )
    scenario.existing_types = {"id": target}
    return scenario


@pytest.mark.parametrize(
    ("native", "target", "values", "error"),
    [
        ("Date32", "Date", [dt.date(1900, 1, 1)], "authoritative date range"),
        (
            "Date32",
            "Date",
            [dt.date(2024, 1, 1), dt.date(2299, 12, 31)],
            "authoritative date range",
        ),
        ("Float64", "Float32", [1e100], "authoritative floating-point domain"),
        ("Float64", "Float32", [0.0, -1e100], "authoritative floating-point domain"),
        ("Int64", "UUID", [1], "authoritative UUID schema"),
        (
            "DateTime64(0)",
            "DateTime",
            [dt.datetime.fromisoformat("1900-01-01")],
            "authoritative timestamp range",
        ),
        (
            "DateTime64(6)",
            "DateTime64(9)",
            [dt.datetime.fromisoformat("2263-01-01")],
            "authoritative timestamp range",
        ),
    ],
)
def test_transfer_rejects_actual_values_outside_a_narrow_existing_ch_domain_before_mutation(
    monkeypatch: pytest.MonkeyPatch,
    native: str,
    target: str,
    values: list[Any],
    error: str,
) -> None:
    scenario = existing_ch_target(monkeypatch, native, target, values)
    spools = track_spools(monkeypatch)

    with pytest.raises(ValueError, match=error):
        scenario.run()

    assert scenario.read_count == 1
    assert "create_target" not in scenario.events
    assert scenario.schemas == []
    assert scenario.loaded == []
    assert scenario.finalizations == 0
    assert spools
    assert all(not path.exists() for path in spools)


@pytest.mark.parametrize(
    ("native", "target", "values"),
    [
        ("Date32", "Date", [dt.date(1970, 1, 1), dt.date(2024, 1, 1)]),
        ("Float64", "Float32", [0.0, 1.0]),
        (
            "DateTime64(6)",
            "DateTime64(9)",
            [dt.datetime.fromisoformat("2024-01-01T00:00:00.123456")],
        ),
    ],
)
def test_transfer_profiles_narrow_date_and_float_domains_even_when_observed_values_fit(
    monkeypatch: pytest.MonkeyPatch,
    native: str,
    target: str,
    values: list[Any],
) -> None:
    scenario = existing_ch_target(monkeypatch, native, target, values)
    spools = track_spools(monkeypatch)

    result = scenario.run(metadata=True)

    assert result.rows == len(values)
    assert result.metadata.input_count == len(values)
    assert scenario.read_count == 1
    assert scenario.schemas == [{"id": target}]
    expected_values = [
        value.replace(tzinfo=dt.timezone.utc) if isinstance(value, dt.datetime) else value
        for value in values
    ]
    assert scenario.loaded == [[(value,) for value in expected_values]]
    assert scenario.events.index("complete:SELECT id FROM source") < scenario.events.index(
        "create_target"
    )
    assert spools
    assert all(not path.exists() for path in spools)


@pytest.mark.parametrize(
    ("native", "target", "values"),
    [
        ("Date", "Date32", [dt.date(1970, 1, 1), dt.date(2149, 6, 6)]),
        ("Float32", "Float64", [0.0, 1.0, float.fromhex("0x1.fffffep+127")]),
        ("Float64", "Float64", [0.0, 1.0, 1e100]),
        (
            "DateTime",
            "DateTime64(6)",
            [
                dt.datetime.fromisoformat("1970-01-01"),
                dt.datetime.fromisoformat("2106-02-07T06:28:15"),
            ],
        ),
    ],
)
def test_transfer_keeps_native_streaming_for_proven_date_and_float_widenings(
    monkeypatch: pytest.MonkeyPatch,
    native: str,
    target: str,
    values: list[Any],
) -> None:
    scenario = existing_ch_target(monkeypatch, native, target, values)
    require_streaming_fast_path(monkeypatch)

    assert scenario.run() == len(values)

    assert scenario.schemas == [{"id": target}]
    expected = [
        value.replace(tzinfo=dt.timezone.utc) if isinstance(value, dt.datetime) else value
        for value in values
    ]
    assert scenario.loaded == [[(value,) for value in expected]]
    assert scenario.read_count == 1
    assert scenario.events.index("create_target") < scenario.events.index(
        "read:SELECT id FROM source"
    )
