from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal
from uuid import UUID

import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends.ch.insert import (
    normalize_batch,
    normalize_typed_batch,
    normalize_typed_row,
)
from analytics_toolkit.sql.backends.temporal_context import localize_value
from analytics_toolkit.sql.backends.type_resolution import (
    ColumnProfile,
    ResolvedColumn,
    resolve_profile,
    validate_profile,
)
from analytics_toolkit.sql.core import exact_values as exact
from analytics_toolkit.sql.core.exact_values import (
    ExactTemporal,
    OrderedMapPairs,
    dataframe_from_columns,
    decimal_shape,
    decode_value,
    encode_value,
    integer_text,
    is_null,
    typed_text,
)
from analytics_toolkit.sql.core.replay_spool import ReplaySpool


@pytest.mark.parametrize("position", [0, 1, 2])
def test_nullable_integer_and_decimal_never_cross_float(position: int) -> None:
    integer = 2**53 + 1
    decimal = Decimal("12345678901234567890.123456789")
    integers = [integer, integer, integer]
    decimals = [decimal, decimal, decimal]
    integers[position] = None
    decimals[position] = None
    frame = pd.DataFrame(
        {"id": pd.Series(integers, dtype=object), "amount": pd.Series(decimals, dtype=object)}
    )
    for result in (
        normalize_batch(frame),
        normalize_typed_batch(
            frame, {"id": "Nullable(UInt64)", "amount": "Nullable(Decimal(38, 9))"}
        ),
    ):
        assert result["id"].tolist() == integers
        assert result["amount"].tolist() == decimals
    assert normalize_typed_row(["amount"], [decimal], {"amount": "Float64"}) == (float(decimal),)
    assert normalize_typed_row(["amount"], [decimal], {"amount": "Decimal(38, 9)"}) == (decimal,)


def test_positional_constructor_preserves_duplicate_labels_and_nested_values() -> None:
    frame = dataframe_from_columns(
        [[None, 2**64 - 1], [[1, None], {"a": Decimal("0.0001")}]], ["same", "same"]
    )
    assert list(frame.columns) == ["same", "same"]
    assert frame.iloc[1, 0] == 2**64 - 1
    assert frame.iloc[0, 1] == [1, None]
    assert frame.iloc[1, 1] == {"a": Decimal("0.0001")}
    assert dataframe_from_columns([], [], index=pd.RangeIndex(3)).shape == (3, 0)


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ([Decimal("0.0001")], (4, 4)),
        ([Decimal("1E+5")], (6, 0)),
        ([Decimal("1.2"), Decimal("0.0034")], (5, 4)),
        ([Decimal("12345678901234567890.123456789")], (29, 9)),
    ],
)
def test_decimal_shape_uses_coefficient_and_exponent(values, expected) -> None:
    assert decimal_shape(values) == expected


@pytest.mark.parametrize("dtype", ["uint8", "uint16", "uint32", "uint64", "UInt64"])
def test_unsigned_dataframe_inference_preserves_clickhouse_family(dtype: str) -> None:
    frame = pd.DataFrame({"id": pd.Series([1, 2], dtype=dtype)})
    ddl = sql.create_table("ch", "default.exact", df=frame, only_generate_sql=True)
    assert "UInt" + str(frame.id.dtype.itemsize * 8) in ddl


def test_dataframe_inference_preserves_decimal_datetime_and_date32() -> None:
    frame = pd.DataFrame(
        {
            "amount": pd.Series([Decimal("1.2"), Decimal("0.0034")], dtype=object),
            "event_at": pd.Series(
                [
                    dt.datetime(2024, 1, 2, 3, 4, 5, tzinfo=dt.timezone.utc).replace(tzinfo=None),
                    dt.datetime(2024, 1, 3, 3, 4, 5, tzinfo=dt.timezone.utc).replace(tzinfo=None),
                ],
                dtype=object,
            ),
            "day": [dt.date(1900, 1, 1), dt.date(2000, 1, 1)],
        }
    )
    ddl = sql.create_table("ch", "default.exact", df=frame, only_generate_sql=True)
    assert "`amount` Decimal(5, 4)" in ddl
    assert "`event_at` DateTime64(6)" in ddl
    assert "`day` Date32" in ddl
    trino = sql.create_table("trino", "iceberg.sandbox.exact", df=frame, only_generate_sql=True)
    assert '"event_at" TIMESTAMP(6)' in trino


def test_exact_tagged_values_and_duplicate_map_keys_round_trip() -> None:
    values = (
        None,
        True,
        2**256 - 1,
        -(2**255),
        Decimal("1E+5"),
        1.25,
        ExactTemporal(-1, 9, "UTC"),
        pd.Timestamp("2024-01-01T01:02:03.123456789+03:00"),
        dt.date(1900, 1, 1),
        UUID(int=1),
        b"\x00\xff",
        "ä",
        [None, (1, 2)],
        {1: Decimal("0.0001")},
        OrderedMapPairs(((1, "first"), (1, "second"))),
    )
    result = decode_value(encode_value(values))
    assert result == values
    assert list(result[-1].items()) == [(1, "first"), (1, "second")]
    assert (
        normalize_typed_row(["map"], [values[-1]], {"map": "Map(Int64, String)"})[0] == values[-1]
    )
    assert not is_null([None])
    assert not is_null(b"")
    with pytest.raises(TypeError, match="Unsupported"):
        encode_value(object())
    with pytest.raises(ValueError, match="Unknown"):
        decode_value(["future"])


def test_typed_text_preserves_exact_values_and_utc_awareness() -> None:
    assert typed_text(None) is None
    assert typed_text(2**256 - 1) == str(2**256 - 1)
    assert typed_text(Decimal("1E+5")) == "100000"
    assert typed_text(Decimal("0.0001")) == "0.0001"
    assert typed_text(b"\x00\xff") == "base64:AP8="
    assert (
        typed_text(pd.Timestamp("2024-01-01T03:00:00.123456789+03:00"))
        == "2024-01-01T00:00:00.123456789+00:00"
    )
    payload = typed_text(
        OrderedMapPairs(((1, "x"), (1, None))),
        source_backend="ch",
        source_type="Map(Int64, String)",
    )
    assert '"v":1' in payload
    assert '"map_pairs"' in payload
    assert normalize_typed_row(["data"], [b"\x00\xff"], {"data": "String"}) == (b"\x00\xff",)
    assert normalize_typed_row(["data"], [Decimal("1E+5")], {"data": "String"}) == ("100000",)


@pytest.mark.parametrize("scale", range(13))
@pytest.mark.parametrize("timezone", [None, "UTC", "Europe/Moscow"])
def test_raw_temporal_ticks_text_retains_full_scale_and_timezone(scale, timezone):
    value = ExactTemporal(-1, scale, timezone)
    encoded = encode_value(value)
    assert decode_value(encoded) == value
    text = typed_text(value)
    assert "1969-12-31T23:59:" in text
    if scale:
        assert "." + "9" * scale in text
    if timezone:
        assert text.endswith("+00:00")
    else:
        assert "+00:00" not in text
    assert value.timestamp() is not None


def test_raw_temporal_outside_pandas_nanosecond_range_keeps_ticks() -> None:
    seconds = int(
        (
            dt.datetime(2299, 1, 1, tzinfo=dt.timezone.utc)
            - dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
        ).total_seconds()
    )
    value = ExactTemporal(seconds * 10**7 + 1, 7, "UTC")
    assert value.timestamp() == value
    assert typed_text(value) == "2299-01-01T00:00:00.0000001+00:00"
    assert decode_value(encode_value(value)) == value
    for scale in (-1, exact.MAX_EXACT_SCALE + 1):
        with pytest.raises(ValueError, match="scale"):
            ExactTemporal(1, scale).timestamp()
    with pytest.raises(ValueError, match="timezone"):
        ExactTemporal(0, 0, "Not/A/Timezone").calendar_datetime()


def test_arbitrary_integer_round_trip_does_not_change_python_digit_limits() -> None:
    number = 10**5000 + 123
    for value in (number, -number, 0):
        assert decode_value(encode_value(value)) == value
    text = integer_text(number)
    assert len(text) == 5001
    assert text.startswith("1")
    assert text.endswith("123")
    assert typed_text(-number) == "-" + text


def test_nested_typed_normalization_uses_explicit_types_without_losing_map_pairs() -> None:
    decimal = Decimal("12345678901234567890.123456789")
    value = OrderedMapPairs(((1, decimal), (1, Decimal("0.0001"))))
    assert normalize_typed_row(["v"], [value], {"v": "Map(Int64, Decimal(38, 9))"}) == (value,)
    assert normalize_typed_row(["v"], [[Decimal("1.25")]], {"v": "Array(Float64)"}) == ([1.25],)
    assert normalize_typed_row(["v"], [(decimal, 1)], {"v": "Tuple(Decimal(38, 9), Int64)"}) == (
        (decimal, 1),
    )
    assert normalize_typed_row(["v"], [{1: decimal}], {"v": "Map(Int64, Decimal(38, 9))"}) == (
        {1: decimal},
    )


def test_exact_value_edge_shapes_and_older_pandas_timestamp_compatibility(monkeypatch) -> None:
    pairs = exact.OrderedMap(((1, "a"), (1, "b")))
    assert list(pairs.items()) == [(1, "a"), (1, "b")]
    assert list(pairs.keys()) == [1, 1]
    assert list(pairs.values()) == ["a", "b"]
    map_pairs = OrderedMapPairs(pairs.pairs)
    assert list(map_pairs.keys()) == [1, 1]
    assert list(map_pairs.values()) == ["a", "b"]
    assert encode_value(map_pairs) == encode_value(pairs)
    assert decode_value(encode_value(pd.Timestamp("2024-01-01"))) == pd.Timestamp("2024-01-01")
    assert decode_value(encode_value(memoryview(b"\x00\xff"))) == b"\x00\xff"
    assert typed_text(UUID(int=1)) == str(UUID(int=1))
    assert typed_text(dt.date(1900, 1, 1)) == "1900-01-01"
    assert not is_null(pd.array([1, None]))
    assert exact.dataframe_from_rows(["same", "same"], [(2**64 - 1, Decimal("0.0001"))]).iloc[
        0
    ].tolist() == [2**64 - 1, Decimal("0.0001")]

    def unavailable_timestamp(*args, **kwargs):
        message = "older pandas nanosecond range"
        raise OverflowError(message)

    with monkeypatch.context() as patch:
        patch.setattr(exact.pd, "Timestamp", unavailable_timestamp)
        assert ExactTemporal(0, 6, "UTC").timestamp() == dt.datetime(
            1970, 1, 1, tzinfo=dt.timezone.utc
        )

    def unknown_missing(value):
        message = "opaque backend scalar"
        raise TypeError(message)

    with monkeypatch.context() as patch:
        patch.setattr(exact.pd, "isna", unknown_missing)
        assert not is_null(object())


def test_raw_offset_naive_clock_identity_survives_text_and_spool_tags() -> None:
    value = ExactTemporal(1, 7, "Europe/Moscow", offset_naive=True)
    assert value.isoformat(utc=True) == "1970-01-01T03:00:00.0000001"
    assert typed_text(value) == "1970-01-01T03:00:00.0000001"
    assert value.timestamp().tzinfo is None
    assert value.calendar_datetime(utc=True).tzinfo is None
    assert decode_value(encode_value(value)) == value
    assert decode_value(["ticks", "1", 7, "UTC"]) == ExactTemporal(1, 7, "UTC")


@pytest.mark.parametrize("scale", [10, 11, 12])
@pytest.mark.parametrize("sign", [-1, 1])
def test_subnanosecond_timestamps_only_narrow_when_actual_ticks_are_aligned(scale, sign):
    raw = ExactTemporal(sign, scale, "UTC")
    assert raw.timestamp() is raw
    assert raw.ticks_floor(9) == (-1 if sign < 0 else 0)
    assert raw.ticks_floor(12) == sign * 10 ** (12 - scale)
    assert decode_value(encode_value(raw)) == raw
    assert typed_text(raw) == raw.isoformat(utc=True)

    nanoseconds = sign * 123456789
    aligned = ExactTemporal(nanoseconds * 10 ** (scale - 9), scale, "UTC")
    native = aligned.timestamp()
    assert isinstance(native, pd.Timestamp)
    assert native.value == nanoseconds
    assert native.tzinfo is not None
    assert aligned.scale == scale
    assert aligned.ticks == nanoseconds * 10 ** (scale - 9)
    assert exact.MAX_TEMPORAL_SCALE == 9
    assert exact.MAX_EXACT_SCALE == 12


def test_precision12_wall_provenance_and_old_tick_tags_survive_certified_replay():
    legacy = ExactTemporal(-1, 12, "UTC")
    wall = ExactTemporal(1, 12, wall_time=True)
    pairs = OrderedMapPairs(((legacy, "before"), (wall, [None, legacy])))
    row = (legacy, wall, pairs, None)
    with ReplaySpool(["instant", "wall", "map", "null"]) as spool:
        spool.write_rows([row])
        spool.certify()
        assert spool.row_count == 1
        assert spool.path.read_bytes().startswith(b"ATK-ROWS\x01\n")
        assert list(spool.iter_rows()) == [row]
        assert list(spool.iter_rows()) == [row]
    assert encode_value(legacy) == ["ticks", "-1", 12, "UTC", False]
    assert encode_value(wall) == ["ticks", "1", 12, None, False, True]
    assert decode_value(["ticks", "1", 9, None]) == ExactTemporal(1, 9)
    assert decode_value(["ticks", "1", 9, "UTC", True]) == ExactTemporal(1, 9, "UTC", True)
    payload = json.loads(typed_text(pairs, source_backend="trino", source_type="MAP"))
    assert payload["v"] == 1
    assert decode_value(payload["value"]) == pairs
    assert typed_text(legacy) == "1969-12-31T23:59:59.999999999999+00:00"
    assert typed_text(wall) == "1970-01-01T00:00:00.000000000001"


@pytest.mark.parametrize("backend", ["gp", "trino", "ch"])
def test_precision12_inference_uses_complete_tick_alignment_before_native_or_text(backend):
    aligned = ExactTemporal(123456 * 10**6, 12, "UTC")
    profile = ColumnProfile()
    profile.add(aligned)
    native = resolve_profile(profile, backend)
    assert not native.text_fallback
    assert profile.temporal_scale == 6
    assert (
        native.target_type
        == {
            "gp": "TIMESTAMP WITH TIME ZONE",
            "trino": "TIMESTAMP(6) WITH TIME ZONE",
            "ch": "DateTime64(6, 'UTC')",
        }[backend]
    )

    last = ExactTemporal(1, 12, "UTC")
    profile.add(last)
    fallback = resolve_profile(profile, backend)
    assert profile.temporal_scale == 12
    assert isinstance(profile.minimum_timestamp_ns, int)
    assert profile.minimum_timestamp_ns == 0
    assert fallback.text_fallback
    assert fallback.convert(last) == "1970-01-01T00:00:00.000000000001+00:00"
    assert fallback.convert(None) is None


def test_precision12_explicit_native_schema_retains_ticks_and_strict_existing_precision_rejects():
    value = ExactTemporal(1, 12, "UTC")
    profile = ColumnProfile()
    profile.add(value)
    native = resolve_profile(
        profile,
        "trino",
        authoritative_type="TIMESTAMP(12) WITH TIME ZONE",
        allow_timestamp_precision_loss=False,
    )
    assert not native.text_fallback
    assert native.convert(value) == value
    with pytest.raises(ValueError, match="timestamp precision"):
        resolve_profile(
            profile,
            "gp",
            authoritative_type="TIMESTAMP(6) WITH TIME ZONE",
            allow_timestamp_precision_loss=False,
        )
    narrowed = resolve_profile(profile, "gp", authoritative_type="TIMESTAMP(6) WITH TIME ZONE")
    assert narrowed.convert(value) == dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
    with pytest.raises(ValueError, match="timestamp scale"):
        validate_profile(profile, "DateTime64(12, 'UTC')", "ch")


@pytest.mark.parametrize(
    ("backend", "native"), [("trino", "TIMESTAMP(12) WITH TIME ZONE"), ("ch", "DateTime64(9)")]
)
def test_precision12_wall_input_localizes_once_but_legacy_instant_coordinates_stay_fixed(
    backend, native
):
    zone = "America/New_York"
    raw = ExactTemporal(1, 12, wall_time=True)
    localized = localize_value(raw, native, zone)
    assert localized == ExactTemporal(18000 * 10**12 + 1, 12, zone)
    assert localize_value(localized, native, zone) is localized
    assert raw == ExactTemporal(1, 12, wall_time=True)
    profile = ColumnProfile(destination_timezone=zone, temporal_type=native)
    profile.add(raw)
    assert profile.temporal_scale == 12
    assert profile.minimum_context_timestamp_ns == 18000 * 10**9
    assert isinstance(profile.minimum_context_timestamp_ns, int)
    converted = ResolvedColumn(native, target_backend=backend, destination_timezone=zone).convert(
        raw
    )
    assert converted == (localized if backend == "trino" else ExactTemporal(18000 * 10**9, 9, zone))
    legacy = ExactTemporal(1, 12, None, False)
    assert localize_value(legacy, native, zone) is legacy
    assert not legacy.wall_time
