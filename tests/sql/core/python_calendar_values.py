"""Python calendars are not narrowed by an optional Timestamp representation."""

from __future__ import annotations

import datetime as dt

import pytest
from analytics_toolkit.sql.core.exact_values import decode_value, encode_value, typed_text
from analytics_toolkit.sql.core.replay_spool import ReplaySpool

from tests.sql._support.pandas_calendar_boundary import restrict_exact_timestamp_to_nanoseconds


@pytest.mark.parametrize("year", [1500, 2299])
@pytest.mark.parametrize("aware", [False, True])
def test_ordinary_wide_python_datetime_roundtrips_with_real_nanosecond_container_bounds(
    monkeypatch, year, aware
):
    restrict_exact_timestamp_to_nanoseconds(monkeypatch)
    zone = dt.timezone(dt.timedelta(hours=3, seconds=7), "native-fixed") if aware else None
    value = dt.datetime(year, 6, 7, 12, 34, 56, 123456, tzinfo=zone, fold=1)

    restored = decode_value(encode_value(value))

    assert restored == value
    assert restored.microsecond == 123456
    assert restored.fold == 1
    assert restored.utcoffset() == value.utcoffset()
    assert restored.tzname() == value.tzname()
    expected = value.astimezone(dt.timezone.utc).isoformat() if aware else value.isoformat()
    assert typed_text(value) == expected
    with ReplaySpool(["stamp"]) as spool:
        spool.write_rows([(value,), (None,)])
        spool.certify()
        assert list(spool.iter_rows()) == [(value,), (None,)]


def test_python_calendar_text_normalizes_aware_boundary_without_inventing_or_losing_ticks():
    value = dt.datetime(1, 1, 1, microsecond=1, tzinfo=dt.timezone(dt.timedelta(hours=14)))
    assert typed_text(value) == "0000-12-31T10:00:00.000001+00:00"
    assert value.year == 1
    assert value.microsecond == 1


def test_wide_datetime_text_retains_the_declared_fractional_scale_without_pandas(monkeypatch):
    restrict_exact_timestamp_to_nanoseconds(monkeypatch)
    value = dt.datetime.fromisoformat("1500-01-01T00:00:00.123456")
    assert typed_text(value, fractional_scale=12) == "1500-01-01T00:00:00.123456000000"


def test_legacy_wide_datetime_keeps_custom_fixed_offset_identity(monkeypatch):
    restrict_exact_timestamp_to_nanoseconds(monkeypatch)
    restored = decode_value(["datetime", "1500-01-01T00:00:00.123456+03:00", "native-fixed"])
    assert restored.year == 1500
    assert restored.microsecond == 123456
    assert restored.utcoffset() == dt.timedelta(hours=3)
    assert restored.tzname() == "native-fixed"


def test_wide_calendar_fallback_never_discards_submicrosecond_ticks(monkeypatch):
    restrict_exact_timestamp_to_nanoseconds(monkeypatch)
    with pytest.raises(ValueError, match="exact sub-microsecond") as error:
        decode_value(["datetime", "1500-01-01T00:00:00.123456789", None, 0])
    assert "1500" not in str(error.value)
    assert "123456789" not in str(error.value)
