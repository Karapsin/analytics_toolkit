"""Write-zone validation uses actual target interpretation before publication."""

import datetime as dt

import pandas as pd
import pytest
from analytics_toolkit.sql.backends.temporal_context import effective_timezone, timezone_info
from analytics_toolkit.sql.backends.type_resolution import (
    ColumnProfile,
    ResolvedColumn,
    resolve_profile,
    validate_profile,
)
from analytics_toolkit.sql.core.exact_values import ExactTemporal


@pytest.mark.parametrize(
    ("value", "native", "zone", "ticks", "valid"),
    [
        (dt.date(1970, 1, 1), "DateTime", "Europe/Moscow", -10800 * 10**9, False),
        (dt.date(2106, 2, 7), "DateTime", "America/Los_Angeles", 4294972800 * 10**9, False),
        (
            dt.datetime.fromisoformat("2262-04-11T23:30:00"),
            "DateTime64(9)",
            "America/Los_Angeles",
            9223396200000000000,
            False,
        ),
        (dt.date(2262, 4, 11), "DateTime64(9)", "America/Los_Angeles", 9223311600000000000, True),
        (dt.date(1970, 1, 1), "DateTime", "+03:00", -10800 * 10**9, False),
    ],
)
def test_all_actual_values_profile_in_frozen_destination_timezone(
    value, native, zone, ticks, valid
):
    profile = ColumnProfile(destination_timezone=zone, temporal_type=native)
    profile.add(value)
    assert profile.minimum_context_timestamp_ns == profile.maximum_context_timestamp_ns == ticks
    assert profile.context_value_count == profile.temporal_value_count == 1
    if valid:
        validate_profile(profile, native, "ch", destination_timezone=zone)
        converted = ResolvedColumn(native, target_backend="ch", destination_timezone=zone).convert(
            value
        )
        assert isinstance(converted, dt.datetime)
        assert converted.tzinfo is not None
    else:
        with pytest.raises(ValueError, match="timestamp range"):
            validate_profile(profile, native, "ch", destination_timezone=zone)
        with pytest.raises(ValueError, match="timestamp range"):
            ResolvedColumn(native, target_backend="ch", destination_timezone=zone).convert(value)


def test_explicit_zone_wins_and_raw_or_aware_instants_are_never_shifted_twice():
    zone = "America/Los_Angeles"
    native = "DateTime64(9, 'UTC')"
    value = dt.datetime.fromisoformat("2262-04-11T23:30:00")
    profile = ColumnProfile(destination_timezone=zone, temporal_type=native)
    profile.add(value)
    validate_profile(profile, native, "ch", destination_timezone=zone)
    assert ResolvedColumn(native, target_backend="ch", destination_timezone=zone).convert(
        value
    ) == value.replace(tzinfo=dt.timezone.utc)
    raw = ExactTemporal(2**63 - 1, 9)
    profile = ColumnProfile(destination_timezone=zone)
    profile.add(raw)
    profile.add(dt.datetime.fromisoformat("2024-01-01T00:00:00"))
    validate_profile(profile, "DateTime64(9)", "ch", destination_timezone=zone)
    assert (
        ResolvedColumn("DateTime64(9)", target_backend="ch", destination_timezone=zone).convert(raw)
        is raw
    )
    aware = value.replace(tzinfo=dt.timezone.utc)
    assert (
        ResolvedColumn("DateTime64(9)", target_backend="ch", destination_timezone=zone).convert(
            aware
        )
        is aware
    )


def test_nested_profiles_and_all_key_merges_keep_exact_timezone_context():
    native = "Array(DateTime)"
    first = ColumnProfile(
        native_type=native, temporal_type=native, destination_timezone="Europe/Moscow"
    )
    first.add([dt.date(2024, 1, 1)])
    last = ColumnProfile(
        native_type=native, temporal_type=native, destination_timezone="Europe/Moscow"
    )
    last.add([dt.date(1970, 1, 1)])
    first.merge(last)
    assert first.children[0].context_value_count == 2
    with pytest.raises(ValueError, match="timestamp range"):
        validate_profile(first, native, "ch", destination_timezone="Europe/Moscow")
    with pytest.raises(ValueError, match="timestamp range"):
        ResolvedColumn(native, target_backend="ch", destination_timezone="Europe/Moscow").convert(
            [dt.date(1970, 1, 1)]
        )
    validate_profile(last, "Array(DateTime('UTC'))", "ch", destination_timezone="Europe/Moscow")


def test_inference_uses_actual_destination_context_for_native_or_text_choice():
    value = pd.Timestamp("2262-04-11T23:30:00.000000001")
    outcomes = {}
    for zone in ["Europe/Moscow", "America/Los_Angeles"]:
        profile = ColumnProfile(destination_timezone=zone)
        profile.add(value)
        result = resolve_profile(profile, "ch", destination_timezone=zone)
        outcomes[zone] = result
    assert outcomes["Europe/Moscow"].target_type == "DateTime64(9)"
    assert outcomes["America/Los_Angeles"].target_type == "String"
    assert outcomes["America/Los_Angeles"].convert(value) == value.isoformat()
    assert outcomes["Europe/Moscow"].metadata()["destination_timezone"] == "Europe/Moscow"


def test_unknown_zone_never_assumes_utc_at_boundary_and_offsets_are_exact():
    profile = ColumnProfile()
    profile.add(dt.date(1970, 1, 1))
    with pytest.raises(ValueError, match="Resolve destination timezone"):
        validate_profile(profile, "DateTime", "ch")
    assert timezone_info("-03:15:30").utcoffset(None) == -dt.timedelta(
        hours=3, minutes=15, seconds=30
    )
    assert effective_timezone("DateTime('UTC')", "Europe/Moscow") == "UTC"
    for zone in ["+03:60", "+03:00:60", "Unknown/Zone"]:
        with pytest.raises(ValueError, match="timezone"):
            timezone_info(zone)


@pytest.mark.parametrize(
    ("wall", "message"),
    [("2024-03-10T02:30:00", "Nonexistent"), ("2024-11-03T01:30:00", "Ambiguous")],
)
def test_ambiguous_and_nonexistent_naive_wall_times_fail_closed(wall, message):
    value = dt.datetime.fromisoformat(wall)
    profile = ColumnProfile(destination_timezone="America/Los_Angeles")
    profile.add(value)
    assert profile.wall_time_error is not None
    inferred = resolve_profile(profile, "ch")
    assert inferred.text_fallback
    assert inferred.convert(value) == value.isoformat()
    with pytest.raises(ValueError, match=message):
        validate_profile(profile, "DateTime64(6)", "ch", destination_timezone="America/Los_Angeles")
    with pytest.raises(ValueError, match=message):
        ResolvedColumn(
            "DateTime64(6)", target_backend="ch", destination_timezone="America/Los_Angeles"
        ).convert(value)
    for fold in [0, 1]:
        aware = value.replace(tzinfo=timezone_info("America/Los_Angeles"), fold=fold)
        if message == "Ambiguous":
            assert (
                ResolvedColumn(
                    "DateTime64(6)", target_backend="ch", destination_timezone="America/Los_Angeles"
                ).convert(aware)
                is aware
            )


def test_canonical_iana_rules_include_future_dst_and_historical_offsets():
    zone = timezone_info("America/Los_Angeles")
    assert dt.datetime(2262, 7, 1, tzinfo=zone).utcoffset() == -dt.timedelta(hours=7)
    assert dt.datetime(2262, 1, 1, tzinfo=zone).utcoffset() == -dt.timedelta(hours=8)
    assert dt.datetime(1970, 7, 1, tzinfo=zone).utcoffset() == -dt.timedelta(hours=7)


def test_python38_backport_import_keeps_the_same_canonical_policy():
    import subprocess  # noqa: PLC0415 - isolate optional-import compatibility from pandas caches
    import sys  # noqa: PLC0415 - current test interpreter

    from analytics_toolkit.sql.core import temporal_zones  # noqa: PLC0415 - source module

    script = """
import importlib, runpy, sys
from types import SimpleNamespace
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
original = importlib.import_module
calls = []
def compatibility_import(name, *args, **kwargs):
    if name == 'zoneinfo':
        calls.append(name)
        raise ImportError('stdlib ZoneInfo unavailable on Python3.8')
    if name == 'backports.zoneinfo':
        calls.append(name)
        return SimpleNamespace(ZoneInfo=ZoneInfo, ZoneInfoNotFoundError=ZoneInfoNotFoundError)
    return original(name, *args, **kwargs)
importlib.import_module = compatibility_import
module = runpy.run_path(sys.argv[1])
assert calls == ['zoneinfo', 'backports.zoneinfo']
assert module['TIMEZONE_POLICY_VERSION'] == 1
assert module['timezone_info']('UTC').key == 'UTC'
"""
    result = subprocess.run(
        [sys.executable, "-c", script, temporal_zones.__file__],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_nested_inference_inherits_exact_profile_destination_context():
    value = pd.Timestamp("2262-04-11T23:30:00.000000001")
    profile = ColumnProfile(destination_timezone="Europe/Moscow")
    profile.add([value])
    result = resolve_profile(profile, "ch")
    assert result.target_type == "Array(DateTime64(9))"
    assert result.destination_timezone == "Europe/Moscow"
    converted = result.convert([value])[0]
    assert converted.utcoffset() == dt.timedelta(hours=3)
    assert converted.nanosecond == 1


def test_raw_wrapper_rejects_unsupported_tick_scale_before_context_profile():
    profile = ColumnProfile(destination_timezone="UTC")
    with pytest.raises(ValueError, match="Raw timestamp scale"):
        profile.add(ExactTemporal(1, 10))


def test_outside_python_utc_calendar_cannot_certify_native_clickhouse_type():
    value = dt.date(1, 1, 1)
    profile = ColumnProfile(destination_timezone="+14:00")
    profile.add(value)
    inferred = resolve_profile(profile, "ch", source_type="DATE", source_backend="gp")
    assert inferred.text_fallback
    assert inferred.convert(value) == "0001-01-01"


def test_portable_zone_loader_fallback_in_local_namespace(monkeypatch):
    import importlib  # noqa: PLC0415 - scoped runtime-loader seam
    import runpy  # noqa: PLC0415 - execute unchanged portable loader locally

    from analytics_toolkit.sql.core import temporal_zones  # noqa: PLC0415 - real runtime module

    runtime_module = temporal_zones._zoneinfo
    utc = temporal_zones.ZoneInfo("UTC")
    original = importlib.import_module
    attempts = []

    def portable_import(name, *args, **kwargs):
        if name == "zoneinfo":
            attempts.append(name)
            msg = "stdlib timezone module unavailable"
            raise ImportError(msg)
        if name == "backports.zoneinfo":
            attempts.append(name)
            return runtime_module
        return original(name, *args, **kwargs)

    with monkeypatch.context() as scoped:
        scoped.setattr(importlib, "import_module", portable_import)
        namespace = runpy.run_path(temporal_zones.__file__)
    assert attempts == ["zoneinfo", "backports.zoneinfo"]
    assert namespace["canonical_named_zone"]("UTC") is utc
    assert importlib.import_module is original
    # The real module and pandas retain their original timezone policy/cache.
    assert temporal_zones.canonical_named_zone("UTC") is utc
    assert pd.Timestamp("2024-01-01").tz_localize(utc).utcoffset() == dt.timedelta(0)


def test_pandas_timestamp_localization_keeps_unknown_and_aware_identity():
    from analytics_toolkit.sql.backends.temporal_context import (  # noqa: PLC0415 - scalar context contract
        localize_value,
    )

    naive = pd.Timestamp("2024-01-01T12:00:00.000000001")
    assert localize_value(naive, "DateTime64(9)") is naive
    aware = naive.tz_localize("UTC")
    assert localize_value(aware, "DateTime64(9)", "Europe/Moscow") is aware
    localized = localize_value(naive, "DateTime64(9)", "Europe/Moscow")
    assert localized.nanosecond == 1
    assert localized.utcoffset() == dt.timedelta(hours=3)
    assert localized.tz_convert("UTC").hour == 9
