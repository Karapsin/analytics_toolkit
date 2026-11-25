"""Final native map identity includes every comparable key component."""

import datetime as dt
from uuid import UUID

import numpy as np
import pandas as pd
import pytest
from analytics_toolkit.sql.backends.map_values import converted_map
from analytics_toolkit.sql.backends.nested_values import row_items
from analytics_toolkit.sql.backends.trino.parameters import normalize_value, timestamp_text
from analytics_toolkit.sql.backends.type_resolution import (
    ColumnProfile,
    ResolvedColumn,
    validate_profile,
)
from analytics_toolkit.sql.core.exact_values import ExactTemporal, OrderedMapPairs
from analytics_toolkit.sql.core.temporal_zones import canonical_named_zone

IDENTIFIER = "abcdefab-cdef-abcd-efab-cdefabcdefab"


@pytest.mark.parametrize(
    ("native", "source"),
    [
        ("MAP(ROW(k CHAR(2)), VARCHAR)", {("a",): "first", ("a ",): "second"}),
        ("MAP(ROW(k REAL), VARCHAR)", {(16777216.0,): "first", (16777217.0,): "second"}),
        ("MAP(ROW(k UUID), VARCHAR)", {(IDENTIFIER,): "first", (IDENTIFIER.upper(),): "second"}),
        ("MAP(ROW(k ROW(inner_key CHAR(2))), VARCHAR)", {(("a",),): "first", (("a ",),): "second"}),
        ("MAP(ARRAY(ROW(k CHAR(2))), VARCHAR)", {(("a",),): "first", (("a ",),): "second"}),
    ],
)
def test_composite_key_collisions_are_detected_before_materialization(native, source):
    with pytest.raises(ValueError, match="Converted map keys are not unique"):
        ResolvedColumn(native, target_backend="trino").convert(source)


def test_valid_recursive_row_keys_keep_values_and_caller_input_unchanged():
    source = {(("a", IDENTIFIER), 16777216.0): "first", (("b", IDENTIFIER), 16777217.0): "second"}
    native = "MAP(ROW(k ROW(c CHAR(2), id UUID), r REAL), VARCHAR)"
    result = ResolvedColumn(native, target_backend="trino").convert(source)
    assert result == {
        (("a", UUID(IDENTIFIER)), 16777216.0): "first",
        (("b", UUID(IDENTIFIER)), 16777217.0): "second",
    }
    assert next(iter(source))[0][1] == IDENTIFIER


@pytest.mark.parametrize(
    ("native", "source"),
    [
        ("MAP(ROW(k VARCHAR), VARCHAR)", {(None,): "value"}),
        ("MAP(ROW(k ROW(inner_key VARCHAR)), VARCHAR)", {((None,),): "value"}),
        ("MAP(ARRAY(VARCHAR), VARCHAR)", OrderedMapPairs((([None], "value"),))),
    ],
)
def test_trino_composite_indeterminate_keys_reject_before_driver(native, source):
    with pytest.raises(ValueError, match="NULL components"):
        ResolvedColumn(native, target_backend="trino").convert(source)


def test_clickhouse_nested_null_keys_and_converted_duplicate_pairs_are_retained():
    source = OrderedMapPairs(
        (((None, IDENTIFIER), "first"), ((None, IDENTIFIER.upper()), "second"))
    )
    result = ResolvedColumn(
        "Map(Tuple(Nullable(Int64), UUID), String)", target_backend="ch"
    ).convert(source)
    assert isinstance(result, OrderedMapPairs)
    assert list(result.items()) == [
        ((None, UUID(IDENTIFIER)), "first"),
        ((None, UUID(IDENTIFIER)), "second"),
    ]
    arrays = ResolvedColumn("Map(Array(Nullable(Int64)), String)", target_backend="ch").convert(
        OrderedMapPairs((([None], "first"), ([None], "second")))
    )
    assert list(arrays.items()) == [([None], "first"), ([None], "second")]


@pytest.mark.parametrize("value", [(1, 2), (), "x", {"other": 1}, np.array([[1]])])
def test_row_conversion_never_discards_missing_or_extra_fields(value):
    with pytest.raises(ValueError, match="Row"):
        ResolvedColumn("ROW(k BIGINT)", target_backend="trino").convert(value)


def test_array_key_without_supported_hashable_mapping_is_rejected_explicitly():
    with pytest.raises(ValueError, match="supported hashable representation"):
        ResolvedColumn("MAP(ARRAY(BIGINT), VARCHAR)", target_backend="trino").convert(
            {(1,): "value"}
        )
    with pytest.raises(ValueError, match="positional sequence"):
        converted_map([(1, "value")], "trino", mapping_input=False, key_type="ARRAY(BIGINT)")
    with pytest.raises(ValueError, match="unsupported native key structure"):
        converted_map([({}, "value")], "trino", mapping_input=False, key_type="MAP(BIGINT, BIGINT)")
    assert row_items(np.array([2**63 + 1], dtype=np.uint64), [("id", "NUMERIC")]) == [2**63 + 1]


def test_nonnullable_composite_fields_and_ambiguous_named_arity_fail_closed():
    with pytest.raises(ValueError, match="NULL components"):
        ResolvedColumn("Map(Tuple(Int64), String)", target_backend="ch").convert({(None,): "value"})
    with pytest.raises(ValueError, match="Row fields"):
        row_items({"id": 1}, [("id", "BIGINT"), ("id", "BIGINT")])


def test_time_key_payload_never_loses_sql_distinct_walls_when_python_keys_round_equal():
    first = dt.time(12, 0, 0, 123400, tzinfo=dt.timezone.utc)
    second = dt.time(13, 0, 0, 123499, tzinfo=dt.timezone(dt.timedelta(hours=1)))
    source = {first: "first", second: "second"}
    assert first != second
    assert len(source) == 2
    conversion = ResolvedColumn("TIME(3)", target_backend="trino")
    assert conversion.convert(first) == conversion.convert(second)

    result = ResolvedColumn("MAP(TIME(3), VARCHAR)", target_backend="trino").convert(source)

    pairs = list(result.items())
    assert len(pairs) == 2
    assert [key.replace(tzinfo=None) for key, _value in pairs] == [
        dt.time(12, 0, 0, 123000),
        dt.time(13, 0, 0, 123000),
    ]
    assert [value for _key, value in pairs] == ["first", "second"]
    assert len(source) == 2


@pytest.mark.parametrize(
    ("native", "shape"),
    [
        ("MAP(TIME(6), VARCHAR)", "scalar"),
        ("MAP(ROW(clock TIME(6)), VARCHAR)", "row"),
        ("MAP(ROW(id BIGINT, clock TIME(6)), VARCHAR)", "id-row"),
        ("MAP(ROW(nested ROW(clock TIME(6))), VARCHAR)", "nested-row"),
    ],
)
def test_time_wall_key_collisions_follow_the_declared_cast_inside_scalar_and_row_keys(
    native, shape
):
    first = dt.time(12, tzinfo=dt.timezone.utc)
    second = dt.time(12, tzinfo=dt.timezone(dt.timedelta(hours=1)))
    if shape == "row":
        first, second = (first,), (second,)
    elif shape == "id-row":
        first, second = (7, first), (7, second)
    elif shape == "nested-row":
        first, second = ((first,),), ((second,),)
    source = {first: "first", second: "second"}
    assert len(source) == 2

    with pytest.raises(ValueError, match="Converted map keys are not unique"):
        ResolvedColumn(native, target_backend="trino").convert(source)


@pytest.mark.parametrize("row", [False, True])
def test_zoned_time_keys_compare_utc_time_of_day_modulo_midnight(row):
    first = dt.time(0, 30, tzinfo=dt.timezone(dt.timedelta(hours=1)))
    second = dt.time(23, 30, tzinfo=dt.timezone.utc)
    assert first != second
    if row:
        first, second = (first,), (second,)
    key_type = "ROW(clock TIME(6) WITH TIME ZONE)" if row else "TIME(6) WITH TIME ZONE"
    source = OrderedMapPairs(((first, "first"), (second, "second")))

    with pytest.raises(ValueError, match="Converted map keys are not unique"):
        ResolvedColumn(f"MAP({key_type}, VARCHAR)", target_backend="trino").convert(source)


@pytest.mark.parametrize("zoned", [False, True])
def test_time_identity_recurses_through_hashable_array_key_payloads(zoned):
    if zoned:
        first = dt.time(0, 30, tzinfo=dt.timezone(dt.timedelta(hours=1)))
        second = dt.time(23, 30, tzinfo=dt.timezone.utc)
    else:
        first = dt.time(12, tzinfo=dt.timezone.utc)
        second = dt.time(12, tzinfo=dt.timezone(dt.timedelta(hours=1)))
    suffix = " WITH TIME ZONE" if zoned else ""

    with pytest.raises(ValueError, match="Converted map keys are not unique"):
        converted_map(
            [((first,), "first"), ((second,), "second")],
            "trino",
            mapping_input=False,
            key_type=f"ARRAY(TIME(6){suffix})",
        )


@pytest.mark.parametrize("row", [False, True])
def test_naive_to_zoned_time_map_keys_require_an_explicit_offset_policy(row):
    first = dt.time(12)
    second = dt.time(12, tzinfo=dt.timezone.utc)
    if row:
        first, second = (first,), (second,)
    key_type = "ROW(clock TIME(6) WITH TIME ZONE)" if row else "TIME(6) WITH TIME ZONE"
    source = OrderedMapPairs(((first, "naive"), (second, "aware")))

    with pytest.raises(ValueError, match=r"offset|timezone|session"):
        ResolvedColumn(f"MAP({key_type}, VARCHAR)", target_backend="trino").convert(source)


@pytest.mark.parametrize(
    ("native", "source"),
    [
        ("MAP(TIME(6), VARCHAR)", {dt.time(12): "first", dt.time(13): "second"}),
        (
            "MAP(TIME(6) WITH TIME ZONE, VARCHAR)",
            {
                dt.time(12, tzinfo=dt.timezone.utc): "first",
                dt.time(12, tzinfo=dt.timezone(dt.timedelta(hours=1))): "second",
            },
        ),
        (
            "MAP(ROW(id BIGINT, clock TIME(6)), VARCHAR)",
            {
                (1, dt.time(12, tzinfo=dt.timezone.utc)): "first",
                (2, dt.time(12, tzinfo=dt.timezone(dt.timedelta(hours=1)))): "second",
            },
        ),
    ],
)
def test_distinct_native_time_keys_keep_every_mapping_entry(native, source):
    result = ResolvedColumn(native, target_backend="trino").convert(source)

    assert isinstance(result, dict)
    assert len(result) == 2
    assert list(result.values()) == ["first", "second"]
    assert len(source) == 2


def test_distinct_hashable_array_time_keys_keep_their_supported_payload_representation():
    first = dt.time(12, tzinfo=dt.timezone.utc)
    second = dt.time(14, tzinfo=dt.timezone(dt.timedelta(hours=1)))
    source = [((first,), "first"), ((second,), "second")]

    result = converted_map(source, "trino", mapping_input=True, key_type="ARRAY(TIME(6))")

    assert result == {(dt.time(12),): "first", (dt.time(14),): "second"}
    assert source == [((first,), "first"), ((second,), "second")]


def test_zoned_time_map_payload_keeps_each_explicit_offset_and_wall_clock():
    first = dt.time(12, tzinfo=dt.timezone.utc)
    second = dt.time(12, tzinfo=dt.timezone(dt.timedelta(hours=5, minutes=30)))
    source = {first: "first", second: "second"}

    result = ResolvedColumn("MAP(TIME(6) WITH TIME ZONE, VARCHAR)", target_backend="trino").convert(
        source
    )

    assert isinstance(result, dict)
    assert list(result.values()) == ["first", "second"]
    keys = list(result)
    assert [key.replace(tzinfo=None) for key in keys] == [dt.time(12), dt.time(12)]
    assert [key.utcoffset() for key in keys] == [dt.timedelta(), dt.timedelta(hours=5, minutes=30)]


def test_time_map_profile_uses_sql_wall_identity_instead_of_python_aware_equality():
    first = dt.time(12, tzinfo=dt.timezone.utc)
    second = dt.time(13, tzinfo=dt.timezone(dt.timedelta(hours=1)))
    assert first == second
    source = OrderedMapPairs(((first, "first"), (second, "second")))
    profile = ColumnProfile(native_type="MAP(TIME(6), VARCHAR)", source_backend="trino")

    profile.add(source)

    assert not profile.map_duplicate_keys
    assert profile.map_comparable_keys
    assert profile.children[0].count == 2
    validate_profile(profile, "MAP(TIME(6), VARCHAR)", "trino")


def test_time_map_profile_flags_true_post_cast_wall_key_collisions():
    first = dt.time(12, tzinfo=dt.timezone.utc)
    second = dt.time(12, tzinfo=dt.timezone(dt.timedelta(hours=1)))
    assert first != second
    profile = ColumnProfile(native_type="MAP(TIME(6), VARCHAR)", source_backend="trino")

    profile.add({first: "first", second: "second"})

    assert profile.map_duplicate_keys
    with pytest.raises(ValueError, match="keys in the authoritative map schema"):
        validate_profile(profile, "MAP(TIME(6), VARCHAR)", "trino")


def test_time_map_profile_keeps_raw_precision_despite_rounded_identity_conversion():
    native = "MAP(TIME(3), VARCHAR)"
    profile = ColumnProfile(native_type=native, source_backend="trino")

    profile.add({dt.time(12, 0, 0, 123456): "value"})

    assert not profile.map_duplicate_keys
    assert profile.children[0].temporal_scale == 6
    with pytest.raises(ValueError, match="precision"):
        validate_profile(profile, native, "trino", allow_timestamp_precision_loss=False)


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (ExactTemporal(1, 0, "UTC"), ExactTemporal(1000, 3, "UTC")),
        (ExactTemporal(1, 0, "UTC"), ExactTemporal(1, 0, "Europe/Paris")),
    ],
    ids=["equal-ticks-at-different-scales", "utc-and-cet-same-instant"],
)
def test_timestamp_instant_aliases_reject_before_late_parameter_string_normalization(first, second):
    assert first != second
    source = {first: "first", second: "second"}
    native = "MAP(TIMESTAMP(12) WITH TIME ZONE, VARCHAR)"

    with pytest.raises(ValueError, match="Converted map keys are not unique"):
        ResolvedColumn(native, target_backend="trino").convert(source)


def test_timestamp_dst_fold_keys_preserve_distinct_sql_instants_despite_python_equality():
    zone = canonical_named_zone("America/New_York")
    first = dt.datetime(2025, 11, 2, 1, 30, tzinfo=zone, fold=0)
    second = dt.datetime(2025, 11, 2, 1, 30, tzinfo=zone, fold=1)
    assert first == second
    source = OrderedMapPairs(((first, "first"), (second, "second")))
    native = "MAP(TIMESTAMP(12) WITH TIME ZONE, VARCHAR)"

    result = ResolvedColumn(native, target_backend="trino").convert(source)

    assert len(result) == 2
    assert all(not isinstance(key, str) for key in result)
    assert normalize_value(result, native) == {
        "2025-11-02 05:30:00.000000000000 UTC": "first",
        "2025-11-02 06:30:00.000000000000 UTC": "second",
    }


@pytest.mark.parametrize("nested", [False, True])
def test_timestamp_instant_identity_recurses_through_row_keys(nested):
    first = ExactTemporal(1, 0, "UTC")
    second = ExactTemporal(1000, 3, "UTC")
    if nested:
        first, second = ((first,),), ((second,),)
        key_type = "ROW(outer_key ROW(stamp TIMESTAMP(12) WITH TIME ZONE))"
    else:
        first, second = (first,), (second,)
        key_type = "ROW(stamp TIMESTAMP(12) WITH TIME ZONE)"

    with pytest.raises(ValueError, match="Converted map keys are not unique"):
        ResolvedColumn(f"MAP({key_type}, VARCHAR)", target_backend="trino").convert(
            {first: "first", second: "second"}
        )


def test_timestamp_instant_identity_recurses_through_hashable_array_key_payloads():
    first = (ExactTemporal(1, 0, "UTC"),)
    second = (ExactTemporal(1000, 3, "UTC"),)

    with pytest.raises(ValueError, match="Converted map keys are not unique"):
        converted_map(
            [(first, "first"), (second, "second")],
            "trino",
            mapping_input=False,
            key_type="ARRAY(TIMESTAMP(12) WITH TIME ZONE)",
        )


@pytest.mark.parametrize(
    ("first", "second", "key_type"),
    [
        (
            ExactTemporal(1, 0, "UTC"),
            pd.Timestamp("1970-01-01T00:00:01+00:00"),
            "TIMESTAMP(12) WITH TIME ZONE",
        ),
        (
            ExactTemporal(1, 0, "UTC"),
            dt.datetime(1970, 1, 1, 0, 0, 1, tzinfo=dt.timezone.utc),
            "TIMESTAMP(12) WITH TIME ZONE",
        ),
        (
            ExactTemporal(1, 0),
            np.datetime64("1970-01-01T00:00:01", "ns"),
            "TIMESTAMP(12)",
        ),
    ],
    ids=["exact-and-pandas", "exact-and-python", "exact-and-numpy-naive"],
)
def test_timestamp_map_identity_compares_existing_temporal_carriers_by_native_value(
    first, second, key_type
):
    source = OrderedMapPairs(((first, "first"), (second, "second")))

    with pytest.raises(ValueError, match="Converted map keys are not unique"):
        ResolvedColumn(f"MAP({key_type}, VARCHAR)", target_backend="trino").convert(source)


def test_distinct_nanosecond_timestamp_keys_keep_native_values_until_parameter_conversion():
    first = pd.Timestamp("2025-01-01T00:00:00.123456789+05:30")
    second = ExactTemporal(first.tz_convert("UTC").value + 1, 9, "UTC")
    source = {first: "first", second: "second"}
    native = "MAP(TIMESTAMP(12) WITH TIME ZONE, VARCHAR)"

    result = ResolvedColumn(native, target_backend="trino").convert(source)

    assert len(result) == 2
    assert all(not isinstance(key, str) for key in result)
    assert normalize_value(result, native) == {
        "2024-12-31 18:30:00.123456789000 UTC": "first",
        "2024-12-31 18:30:00.123456790000 UTC": "second",
    }
    assert first.utcoffset() == dt.timedelta(hours=5, minutes=30)
    assert len(source) == 2


def test_timestamp_map_profile_flags_equal_instants_despite_distinct_raw_tick_scales():
    native = "MAP(TIMESTAMP(12) WITH TIME ZONE, VARCHAR)"
    profile = ColumnProfile(native_type=native, source_backend="trino")
    profile.add({ExactTemporal(1, 0, "UTC"): "first", ExactTemporal(1000, 3, "UTC"): "second"})

    assert profile.map_duplicate_keys
    with pytest.raises(ValueError, match="keys in the authoritative map schema"):
        validate_profile(profile, native, "trino")


def test_timestamp_map_profile_keeps_dst_fold_instants_distinct_without_losing_raw_precision():
    zone = canonical_named_zone("America/New_York")
    first = dt.datetime(2025, 11, 2, 1, 30, 0, 123456, tzinfo=zone, fold=0)
    second = first.replace(fold=1)
    native = "MAP(TIMESTAMP(12) WITH TIME ZONE, VARCHAR)"
    profile = ColumnProfile(native_type=native, source_backend="trino")
    profile.add(OrderedMapPairs(((first, "first"), (second, "second"))))

    assert not profile.map_duplicate_keys
    assert profile.map_comparable_keys
    assert profile.children[0].temporal_scale == 6
    validate_profile(profile, native, "trino")


@pytest.mark.parametrize(
    ("raw", "expected_wall", "offset"),
    [
        ("2025-01-01T12:00:00.123456789+00:00", "12:00:00", dt.timedelta()),
        (
            "2025-01-01T12:00:00.123456789+05:30",
            "06:30:00",
            dt.timedelta(hours=5, minutes=30),
        ),
    ],
)
@pytest.mark.parametrize("precision", [6, 12])
def test_timestamp_map_frozen_context_keeps_actual_parameter_projection_idempotent(
    raw, expected_wall, offset, precision
):
    first = pd.Timestamp(raw)
    source = {first: "value"}
    native = f"MAP(TIMESTAMP({precision}), VARCHAR)"
    conversion = ResolvedColumn(
        native, target_backend="trino", destination_timezone="America/New_York"
    )

    result = conversion.convert(source)
    repeated = conversion.convert(result)

    assert len(result) == 1
    assert len(repeated) == 1
    assert all(not isinstance(key, str) for key in result)
    fraction = "123456" if precision == 6 else "123456789000"
    expected = f"2025-01-01 {expected_wall}.{fraction}"
    for version in (result, repeated):
        normalized = normalize_value(version, native)
        assert len(normalized) == 1
        key = next(iter(normalized))
        text = key if isinstance(key, str) else timestamp_text(key, precision)
        assert (text[:-4] if text.endswith(" UTC") else text) == expected
        assert list(normalized.values()) == ["value"]
    assert list(source) == [first]
    assert first.utcoffset() == offset
