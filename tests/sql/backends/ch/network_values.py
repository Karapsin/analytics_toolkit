"""ClickHouse IP values retain native address width, identity and null semantics."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from enum import Enum
from ipaddress import IPv4Address, IPv6Address
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd
import pytest
from analytics_toolkit.sql.backends.ch.insert import normalize_typed_row
from analytics_toolkit.sql.backends.ch.network_values import (
    convert_network_value,
    declared_network_fits,
    is_network_type,
    key_validation_frames,
    network_key_identity,
    observe_network_value,
    validate_network_profile,
)
from analytics_toolkit.sql.backends.conversion_profiles import validate_converted_rows
from analytics_toolkit.sql.backends.native_values import dataframe_key_validation_frames
from analytics_toolkit.sql.backends.type_resolution import (
    ResolvedColumn,
    declared_domain_fits,
    resolve_profile,
    validate_profile,
)
from analytics_toolkit.sql.backends.value_profiles import ColumnProfile
from analytics_toolkit.sql.core.exact_values import OrderedMapPairs

if TYPE_CHECKING:
    from collections.abc import Iterable

_V4 = IPv4Address("192.0.2.1")
_V6 = IPv6Address("2001:db8::1")
_MAPPED = IPv6Address("::ffff:192.0.2.1")
_SECRET = "private-invalid-address"


class _LogicalAddress(Enum):
    V4 = "192.0.2.1"
    V6 = IPv6Address("2001:db8::1")
    NULL = None


def _profile(values: Iterable[Any], native: str, *, backend: str = "ch") -> ColumnProfile:
    profile = ColumnProfile(native_type=native, source_backend=backend)
    for value in values:
        profile.add(value)
    return profile


def _strided(packed: bytes) -> memoryview:
    return memoryview(b"".join(bytes((byte, 127)) for byte in packed))[::2]


@pytest.mark.parametrize(
    "native",
    ["IPv4", "IPv6", "Nullable(IPv4)", "LowCardinality(IPv6)", "LowCardinality(Nullable(IPv4))"],
)
def test_network_declarations_preserve_supported_scalar_wrappers(native):
    assert is_network_type(native)
    value = "192.0.2.1" if "IPv4" in native else "2001:db8::1"
    expected = _V4 if "IPv4" in native else _V6
    assert convert_network_value(value, native) == expected
    profile = _profile([value], native)
    assert validate_network_profile(profile, native)
    validate_profile(profile, native, "ch")


@pytest.mark.parametrize("native", ["String", "IPv40", "Array(IPv4)", "Tuple(IPv4, IPv6)"])
def test_network_recognition_does_not_register_other_type_families(native):
    assert not is_network_type(native)
    assert not validate_network_profile(ColumnProfile(), native)


@pytest.mark.parametrize(
    "native", ["IPv4(1)", "IPv6()", "Nullable(IPv4, IPv6)", "LowCardinality(IPv6, IPv6)"]
)
@pytest.mark.parametrize("values", [[], [None], ["192.0.2.1"]])
def test_invalid_leaf_arguments_and_wrapper_arity_cannot_be_certified(native, values):
    with pytest.raises(ValueError, match=r"argument|nonnullable|declaration"):
        validate_profile(_profile(values, native), native, "ch")


@pytest.mark.parametrize(
    "value",
    [
        "192.0.2.1",
        _V4,
        int(_V4),
        np.uint32(int(_V4)),
        _V4.packed,
        bytearray(_V4.packed),
        memoryview(_V4.packed).cast("H"),
        _strided(_V4.packed),
        _LogicalAddress.V4,
    ],
)
def test_ipv4_supported_forms_become_exact_native_address_objects(value):
    converted = convert_network_value(value, "IPv4")
    assert type(converted) is IPv4Address
    assert converted == _V4
    assert converted.packed == _V4.packed
    assert ResolvedColumn("IPv4", target_backend="ch").convert(value) == _V4
    assert _profile([value], "IPv4").native_constraints_valid


@pytest.mark.parametrize(
    "value",
    [
        "2001:0DB8:0:0:0:0:0:1",
        _V6,
        int(_V6),
        _V6.packed,
        bytearray(_V6.packed),
        memoryview(_V6.packed).cast("H"),
        _strided(_V6.packed),
        _LogicalAddress.V6,
    ],
)
def test_ipv6_supported_forms_become_exact_native_address_objects(value):
    converted = convert_network_value(value, "IPv6")
    assert type(converted) is IPv6Address
    assert converted == _V6
    assert converted.packed == _V6.packed
    assert ResolvedColumn("IPv6", target_backend="ch").convert(value) == _V6
    assert _profile([value], "IPv6").native_constraints_valid


@pytest.mark.parametrize(
    "value",
    [
        "192.0.2.1",
        _V4,
        _V4.packed,
        bytearray(_V4.packed),
        memoryview(_V4.packed).cast("H"),
        _strided(_V4.packed),
    ],
)
def test_explicit_ipv4_forms_promote_to_mapped_ipv6_without_guessing_integer_width(value):
    assert convert_network_value(value, "IPv6") == _MAPPED
    assert network_key_identity(value, "IPv6") == _MAPPED.packed
    assert convert_network_value(1, "IPv6") == IPv6Address("::1")
    assert convert_network_value("0.0.0.1", "IPv6") == IPv6Address("::ffff:0.0.0.1")


@pytest.mark.parametrize(
    ("native", "value", "expected"),
    [
        ("IPv4", 0, IPv4Address(0)),
        ("IPv4", False, IPv4Address(0)),
        ("IPv4", True, IPv4Address(1)),
        ("IPv4", np.bool_(True), IPv4Address(1)),
        ("IPv4", 2**32 - 1, IPv4Address(2**32 - 1)),
        ("IPv6", 0, IPv6Address(0)),
        ("IPv6", False, IPv6Address(0)),
        ("IPv6", True, IPv6Address(1)),
        ("IPv6", np.bool_(True), IPv6Address(1)),
        ("IPv6", 2**128 - 1, IPv6Address(2**128 - 1)),
    ],
)
def test_network_unsigned_integer_boundaries_remain_native(native, value, expected):
    assert convert_network_value(value, native) == expected
    profile = validate_converted_rows(
        [(value,)], [ResolvedColumn(native, target_backend="ch")], backend="ch"
    )[0]
    assert profile.count == 1
    assert profile.native_constraints_valid


@pytest.mark.parametrize("native", ["IPv4", "IPv6"])
@pytest.mark.parametrize("value", [1.0, float("inf"), Decimal(1), "", _SECRET])
def test_non_native_or_malformed_network_values_are_not_stringified(native, value):
    with pytest.raises(ValueError, match="IPv4/IPv6") as caught:
        convert_network_value(value, native)
    assert _SECRET not in str(caught.value)
    assert caught.value.__cause__ is None
    profile = _profile([value], native)
    assert not profile.native_constraints_valid
    with pytest.raises(ValueError, match="IPv4/IPv6"):
        validate_profile(profile, native, "ch")


@pytest.mark.parametrize(
    ("native", "value"),
    [
        ("IPv4", -1),
        ("IPv4", 2**32),
        ("IPv4", _V6),
        ("IPv4", _MAPPED),
        ("IPv4", _V6.packed),
        ("IPv4", b"abc"),
        ("IPv4", b"abcde"),
        ("IPv4", "010.0.0.1"),
        ("IPv4", "256.0.0.1"),
        ("IPv4", " 192.0.2.1"),
        ("IPv6", -1),
        ("IPv6", 2**128),
        ("IPv6", b"abc"),
        ("IPv6", b"a" * 15),
        ("IPv6", b"a" * 17),
        ("IPv6", "::ffff:010.0.0.1"),
    ],
)
def test_network_family_width_and_packed_length_must_match_the_declared_type(native, value):
    with pytest.raises(ValueError, match="IPv4/IPv6"):
        convert_network_value(value, native)
    with pytest.raises(ValueError, match="IPv4/IPv6"):
        validate_converted_rows(
            [(value,)], [ResolvedColumn(native, target_backend="ch")], backend="ch"
        )


@pytest.mark.parametrize(
    "value", ["fe80::1%private-interface", "fe80::1%7", IPv6Address("fe80::1%7")]
)
def test_ipv6_scope_identifiers_cannot_be_discarded(value):
    with pytest.raises(ValueError, match="scope") as caught:
        convert_network_value(value, "IPv6")
    assert "private-interface" not in str(caught.value)


@pytest.mark.parametrize("native", ["IPv4", "IPv6", "LowCardinality(IPv4)"])
def test_nonnullable_network_nulls_fail_before_backend_specific_acceptance(native):
    profile = _profile([None], native)
    with pytest.raises(ValueError, match="NULL"):
        validate_profile(profile, native, "ch")
    with pytest.raises(ValueError, match="NULL"):
        validate_converted_rows(
            [(None,)], [ResolvedColumn(native, target_backend="ch")], backend="ch"
        )


@pytest.mark.parametrize("value", [None, pd.NA, pd.NaT, float("nan"), _LogicalAddress.NULL])
@pytest.mark.parametrize("native", ["Nullable(IPv4)", "LowCardinality(Nullable(IPv6))"])
def test_nullable_network_values_keep_sql_nulls_without_address_constructor_calls(native, value):
    assert convert_network_value(value, native) is None
    profile = validate_converted_rows(
        [(value,)], [ResolvedColumn(native, target_backend="ch")], backend="ch"
    )[0]
    assert (profile.count, profile.null_count) == (1, 1)


def test_full_profile_and_converted_transport_validate_the_last_selected_network_value():
    values = ["::1", _V4.packed, _SECRET]
    profile = _profile(values, "IPv6")
    assert profile.count == 3
    assert not profile.native_constraints_valid
    with pytest.raises(ValueError, match="IPv4/IPv6"):
        validate_profile(profile, "IPv6", "ch")
    scanned = []

    def rows():
        for index, value in enumerate(values):
            scanned.append(index)
            yield (value,)

    with pytest.raises(ValueError, match="IPv4/IPv6"):
        validate_converted_rows(rows(), [ResolvedColumn("IPv6", target_backend="ch")], backend="ch")
    assert scanned == [0, 1, 2]
    merged = _profile(["::1"], "IPv6").merge(_profile([_SECRET], "IPv6"))
    assert merged.count == 2
    assert not merged.native_constraints_valid


def test_gp_domain_names_and_unhinted_profiles_do_not_invoke_clickhouse_ip_policy():
    profile = _profile([_SECRET], "IPv4", backend="gp")
    assert profile.native_constraints_valid
    observe_network_value(profile, _SECRET)
    assert profile.native_constraints_valid
    resolution = ResolvedColumn("IPv4", target_backend="gp", native_assignment_required=True)
    assert resolution.convert(_SECRET) == _SECRET
    converted = validate_converted_rows([(_SECRET,)], [resolution], backend="gp")[0]
    assert converted.native_constraints_valid
    no_hint = ColumnProfile(source_backend="ch")
    observe_network_value(no_hint, _SECRET)
    assert no_hint.native_constraints_valid


@pytest.mark.parametrize(
    ("native", "values", "expected"),
    [
        ("IPv4", [], "IPv4"),
        ("IPv6", [_V6], "IPv6"),
        ("Nullable(IPv4)", [None, pd.NA], "Nullable(IPv4)"),
        ("IPv6", [None], "Nullable(IPv6)"),
    ],
)
def test_declared_network_inference_preserves_native_empty_and_all_null_columns(
    native, values, expected
):
    resolution = resolve_profile(
        _profile(values, native), "ch", source_type=native, source_backend="ch"
    )
    assert resolution.target_type == expected
    assert not resolution.text_fallback


@pytest.mark.parametrize(
    ("source", "target", "expected"),
    [
        ("IPv4", "IPv4", True),
        ("IPv4", "Nullable(IPv4)", True),
        ("IPv4", "IPv6", True),
        ("IPv4", "LowCardinality(Nullable(IPv6))", True),
        ("Nullable(IPv4)", "Nullable(IPv6)", True),
        ("Nullable(IPv4)", "IPv4", False),
        ("Nullable(IPv6)", "IPv6", False),
        ("IPv6", "IPv4", False),
        ("LowCardinality(Nullable(IPv4))", "LowCardinality(IPv6)", False),
    ],
)
def test_declared_network_domain_proof_preserves_nullability_and_address_family(
    source, target, expected
):
    assert declared_domain_fits(source, target, "ch", source_backend="ch") is expected


def test_network_family_proof_is_backend_local_and_source_typed_conversion_is_exact():
    assert declared_network_fits("IPv4", "IPv6")
    assert declared_network_fits("IPv6", "IPv4") is False
    assert declared_network_fits("String", "IPv6") is None
    assert not declared_domain_fits("IPv4", "IPv6", "ch", source_backend="gp")
    typed = ResolvedColumn("IPv6", source_type="IPv4", source_backend="ch", target_backend="ch")
    assert typed.convert(1) == IPv6Address("::ffff:0.0.0.1")
    assert typed.convert(_V4.packed) == _MAPPED
    assert ResolvedColumn("IPv6", target_backend="ch").convert(1) == IPv6Address("::1")


@pytest.mark.parametrize(
    ("native", "value", "expected"),
    [
        ("Array(Nullable(IPv6))", [None, 1, _V4.packed], [None, IPv6Address("::1"), _MAPPED]),
        ("Tuple(IPv4, IPv6)", (int(_V4), _V4), (_V4, _MAPPED)),
        (
            "Map(String, Nullable(IPv6))",
            {"absent": None, "host": _V6.packed},
            {"absent": None, "host": _V6},
        ),
    ],
)
def test_network_values_recurse_through_native_containers(native, value, expected):
    assert ResolvedColumn(native, target_backend="ch").convert(value) == expected
    profile = validate_converted_rows(
        [(value,)], [ResolvedColumn(native, target_backend="ch")], backend="ch"
    )[0]
    assert profile.count == 1


@pytest.mark.parametrize("value", [(_V4,), (_V4, _V6, _V4)])
def test_network_tuple_arity_is_validated_before_positional_conversion(value):
    with pytest.raises(ValueError, match="arity"):
        validate_converted_rows(
            [(value,)], [ResolvedColumn("Tuple(IPv4, IPv6)", target_backend="ch")], backend="ch"
        )


def test_native_network_map_aliases_keep_every_value_and_normal_maps_remain_dicts():
    source = {"0:0:0:0:0:0:0:1": "first", IPv6Address("::1"): "second"}
    resolution = ResolvedColumn("Map(IPv6, String)", target_backend="ch")
    converted = resolution.convert(source)
    assert isinstance(converted, OrderedMapPairs)
    assert list(converted.items()) == [
        (IPv6Address("::1"), "first"),
        (IPv6Address("::1"), "second"),
    ]
    assert len(source) == 2
    profile = validate_converted_rows([(source,)], [resolution], backend="ch")[0]
    assert profile.map_duplicate_keys
    normal = resolution.convert({"::1": "one", "::2": "two"})
    assert isinstance(normal, dict)
    assert normal == {IPv6Address("::1"): "one", IPv6Address("::2"): "two"}
    pairs = OrderedMapPairs((("::1", "one"), ("0:0:0:0:0:0:0:1", "two")))
    assert len(resolution.convert(pairs)) == 2
    with pytest.raises(ValueError, match="NULL"):
        resolution.convert({None: "missing"})


@pytest.mark.parametrize("native_transport", [None, False, True])
def test_native_network_map_enum_keys_are_not_collapsed_before_typed_conversion(native_transport):
    source = {_LogicalAddress.V4: "enum", "192.0.2.1": "text"}
    (normalized,) = normalize_typed_row(
        ["payload"],
        (source,),
        {"payload": "Map(IPv6, String)"},
        native_transport=native_transport,
    )
    assert isinstance(normalized, OrderedMapPairs)
    expected_values = (
        [(_MAPPED, b"enum"), (_MAPPED, b"text")]
        if native_transport is False
        else [(_MAPPED, "enum"), (_MAPPED, "text")]
    )
    assert list(normalized.items()) == expected_values
    assert list(source.items()) == [(_LogicalAddress.V4, "enum"), ("192.0.2.1", "text")]


def test_direct_typed_writer_normalizes_original_network_forms_and_named_tuple_fields():
    schema = {
        "v4": "IPv4",
        "v6": "IPv6",
        "array": "Array(Nullable(IPv6))",
        "tuple": "Tuple(IPv4, IPv6)",
        "named": "Tuple(host IPv6, routes Array(IPv4))",
        "map": "Map(IPv6, String)",
    }
    source = (
        int(_V4),
        1,
        [None, _V4.packed],
        (_V4.packed, _V4),
        {"routes": [int(_V4)], "host": "2001:0db8::1"},
        {"192.0.2.1": "text", _MAPPED: "object"},
    )
    converted = normalize_typed_row(list(schema), source, schema)
    assert converted[:5] == (
        _V4,
        IPv6Address("::1"),
        [None, _MAPPED],
        (_V4, _MAPPED),
        {"host": _V6, "routes": [_V4]},
    )
    assert isinstance(converted[5], OrderedMapPairs)
    assert list(converted[5].items()) == [(_MAPPED, "text"), (_MAPPED, "object")]
    assert source[4] == {"routes": [int(_V4)], "host": "2001:0db8::1"}


def test_network_key_identities_use_declared_native_width_and_reject_sql_null():
    assert network_key_identity("192.0.2.1", "IPv4") == _V4.packed
    assert network_key_identity(_V4.packed, "IPv6") == _MAPPED.packed
    assert network_key_identity(1, "IPv6") == IPv6Address("::1").packed
    assert network_key_identity("0.0.0.1", "IPv6") != network_key_identity(1, "IPv6")
    with pytest.raises(ValueError, match="NULL"):
        network_key_identity(None, "Nullable(IPv6)")


def test_effective_partition_and_composite_network_keys_use_native_identity_without_mutation():
    frame = pd.DataFrame(
        {
            "month": ["jan", "feb", "jan"],
            "id": [1, 1, 1],
            "ip": pd.Series(["192.0.2.1", _V4, _V4.packed], dtype=object),
            "payload": [1, 2, 3],
        }
    )
    original = frame.copy(deep=True)
    keys = ["month", "id", "ip"]
    schema = {"month": "String", "id": "Int64", "ip": "IPv6"}
    frames = list(key_validation_frames(frame, keys, [None, schema, dict(schema)]))
    assert len(frames) == 1
    assert list(frames[0].columns) == keys
    assert frames[0].ip.tolist() == [_MAPPED.packed] * 3
    assert frames[0].duplicated(keys, keep=False).tolist() == [True, False, True]
    assert all(dtype.kind == "O" for dtype in frames[0].dtypes)
    pd.testing.assert_frame_equal(frame, original)


def test_network_key_schema_deduplication_tracks_every_effective_key_conversion():
    frame = pd.DataFrame(
        {
            "ip": pd.Series(["::1", IPv6Address("::1")], dtype=object),
            "stamp": pd.Series(
                [
                    pd.Timestamp("2024-01-01T00:00:00.123456788Z"),
                    pd.Timestamp("2024-01-01T00:00:00.123456789Z"),
                ],
                dtype=object,
            ),
        }
    )
    original = frame.copy(deep=True)
    exact = {"ip": "IPv6", "stamp": "DateTime64(9, 'UTC')"}
    reduced = {"ip": "IPv6", "stamp": "DateTime64(3, 'UTC')"}
    frames = list(key_validation_frames(frame, ["ip", "stamp"], [exact, reduced, dict(exact)]))
    assert len(frames) == 2
    assert frames[0].duplicated(keep=False).tolist() == [False, False]
    assert frames[1].duplicated(keep=False).tolist() == [True, True]
    assert all(dtype.kind == "O" for current in frames for dtype in current.dtypes)
    pd.testing.assert_frame_equal(frame, original)


def test_network_key_views_use_frozen_destination_timezone_and_preserve_caller_values():
    frame = pd.DataFrame(
        {
            "ip": pd.Series(["::1", IPv6Address("::1")], dtype=object),
            "stamp": pd.Series(
                [
                    dt.datetime(2024, 1, 1, 3, 30),  # noqa: DTZ001 -- explicit destination zone.
                    dt.datetime(2024, 1, 1, 0, 30, tzinfo=dt.timezone.utc),
                ],
                dtype=object,
            ),
        }
    )
    original = frame.copy(deep=True)
    schema = {"ip": "IPv6", "stamp": "DateTime64(6)"}
    frames = list(
        dataframe_key_validation_frames(
            frame, ["ip", "stamp"], [schema], "ch", destination_timezone="Europe/Moscow"
        )
    )
    assert len(frames) == 1
    assert frames[0].duplicated(keep=False).tolist() == [True, True]
    utc = list(key_validation_frames(frame, ["ip", "stamp"], [schema], destination_timezone="UTC"))
    assert utc[0].duplicated(keep=False).tolist() == [False, False]
    pd.testing.assert_frame_equal(frame, original)


@pytest.mark.parametrize("keys", [None, [], ["other"]])
def test_no_declared_network_key_keeps_ordinary_key_validation_unchanged(keys):
    frame = pd.DataFrame({"ip": [_SECRET], "other": [1]})
    assert list(key_validation_frames(frame, keys, [{"ip": "IPv4", "other": "Int64"}])) == []
    assert list(key_validation_frames(frame, ["ip"], [None, {"ip": "String"}])) == []


def test_empty_network_key_views_keep_object_dtype_and_native_null_identity():
    frame = pd.DataFrame({"ip": pd.Series(dtype=object), "id": pd.Series(dtype="int64")})
    views = list(key_validation_frames(frame, ["ip", "id"], [{"ip": "IPv4", "id": "Int64"}]))
    assert len(views) == 1
    assert views[0].empty
    assert all(dtype.kind == "O" for dtype in views[0].dtypes)
    nullable = pd.DataFrame({"ip": pd.Series([None, "::1"], dtype=object)})
    view = next(key_validation_frames(nullable, ["ip"], [{"ip": "Nullable(IPv6)"}]))
    assert view.ip.tolist() == [None, IPv6Address("::1").packed]
    assert nullable.ip.tolist() == [None, "::1"]
