"""Native inference certifies actual calendar bounds and complete value profiles."""

import datetime as dt

import pytest
from analytics_toolkit.sql.backends.type_resolution import ColumnProfile, resolve_profile
from analytics_toolkit.sql.core.exact_values import ExactTemporal


@pytest.mark.parametrize(
    ("value", "source", "text"),
    [
        (dt.date(1800, 1, 1), "DATE", "1800-01-01"),
        (
            dt.datetime(2300, 1, 1, tzinfo=dt.timezone.utc),
            "TIMESTAMP(6) WITH TIME ZONE",
            "2300-01-01T00:00:00.000000+00:00",
        ),
        (ExactTemporal(1041379200 * 10**10 + 1, 9, "UTC"), "DateTime64(9, 'UTC')", None),
    ],
)
def test_outside_native_calendar_range_is_exact_text(value, source, text):
    profile = ColumnProfile()
    profile.add(None)
    profile.add(value)
    result = resolve_profile(profile, "ch", source_type=source, source_backend="gp")
    assert result.target_type == "Nullable(String)"
    assert result.text_fallback
    assert result.convert(None) is None
    assert result.convert(value) == (text or value.isoformat(utc=True))


def test_aligned_late_timestamp_tries_exact_lower_precision_native_before_text():
    value = ExactTemporal(10413791999 * 10**9, 9, "UTC")
    profile = ColumnProfile()
    profile.add(value)
    result = resolve_profile(profile, "ch", source_type="DateTime64(9, 'UTC')", source_backend="ch")
    assert result.target_type == "DateTime64(6, 'UTC')"
    assert not result.text_fallback
    assert result.convert(value) == ExactTemporal(value.ticks // 1000, 6, "UTC")


@pytest.mark.parametrize("source", ["DATE", "Date32"])
@pytest.mark.parametrize("with_null", [False, True])
def test_empty_and_all_null_date_sources_keep_native_family(source, with_null):
    profile = ColumnProfile()
    if with_null:
        profile.add(None)
    result = resolve_profile(profile, "ch", source_type=source, source_backend="gp")
    assert result.target_type == (
        "Nullable(Date32)" if with_null or source == "Date32" else "Date32"
    )
    assert not result.text_fallback


@pytest.mark.parametrize("backend", ["gp", "trino", "ch"])
def test_empty_timestamp_defaults_are_legal_native_backend_types(backend):
    result = resolve_profile(
        ColumnProfile(), backend, source_type="TIMESTAMP(6) WITH TIME ZONE", source_backend="gp"
    )
    assert (
        result.target_type
        == {
            "gp": "TIMESTAMP(6) WITH TIME ZONE",
            "trino": "TIMESTAMP(6) WITH TIME ZONE",
            "ch": "DateTime64(6, 'UTC')",
        }[backend]
    )
    assert not result.text_fallback


@pytest.mark.parametrize(("backend", "native"), [("gp", "BYTEA[]"), ("trino", "ARRAY(VARBINARY)")])
def test_native_certification_profiles_exact_recursive_binary_conversions(backend, native):
    profile = ColumnProfile(native_type="Array(String)", source_backend="ch")
    profile.add(["é", b"\xff"])
    result = resolve_profile(profile, backend, source_type="Array(String)", source_backend="ch")
    assert result.target_type == native
    assert not result.text_fallback
    assert result.convert(["é", b"\xff"]) == [b"\xc3\xa9", b"\xff"]


def test_recursive_binary_certification_preserves_greenplum_declared_rank_contract():
    from analytics_toolkit.sql.backends.inferred_types import (  # noqa: PLC0415 - certification boundary
        certify_native_candidate,
    )

    profile = ColumnProfile(native_type="Array(Array(String))", source_backend="ch")
    profile.add([["é"]])
    assert (
        certify_native_candidate(profile, "BYTEA[][]", "gp", "Array(Array(String))", "ch")
        == "BYTEA[][]"
    )
