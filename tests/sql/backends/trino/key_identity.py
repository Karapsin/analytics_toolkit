"""Destination equality composes every leaf of native Trino keys."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pandas as pd
import pytest
from analytics_toolkit.sql.backends.conversion_profiles import validate_converted_rows
from analytics_toolkit.sql.backends.map_values import (
    _key_identity,
    converted_map,
    profile_map_key_identity,
)
from analytics_toolkit.sql.backends.native_values import dataframe_key_validation_frames
from analytics_toolkit.sql.backends.trino.ip_values import (
    _key_identity as ip_key_identity,
)
from analytics_toolkit.sql.backends.trino.ip_values import (
    key_validation_frames as legacy_key_validation_frames,
)
from analytics_toolkit.sql.backends.trino.ip_values import validate_ip_transport_value
from analytics_toolkit.sql.backends.trino.parameters import normalize_value
from analytics_toolkit.sql.backends.type_resolution import ColumnProfile, ResolvedColumn
from analytics_toolkit.sql.core.exact_values import ExactTemporal, OrderedMapPairs
from analytics_toolkit.sql.dml.load.load_df import _validate_dataframe_key_uniqueness

from tests.sql._support.trino_binary_buffers import use_actual_formatter
from tests.sql._support.trino_parameters import render_parameter

_IPV4 = "192.0.2.1"
_MAPPED = "::ffff:192.0.2.1"
_UUID = "abcdefab-cdef-abcd-efab-cdefabcdefab"
_UTC = dt.timezone.utc
_PLUS_THREE = dt.timezone(dt.timedelta(hours=3))


@pytest.mark.parametrize(
    ("sibling_type", "first", "second"),
    [
        ("CHAR(2)", "a", "a "),
        ("CHARACTER(2)", "a", "a "),
        ("REAL", float(2**24), float(2**24 + 1)),
        ("DOUBLE", -0.0, 0.0),
        ("DECIMAL(8, 2)", Decimal("1.0"), Decimal("1.00")),
        ("UUID", _UUID, _UUID.upper()),
        ("TIME(6)", dt.time(12, tzinfo=_UTC), dt.time(12, tzinfo=_PLUS_THREE)),
        ("TIME(6) WITH TIME ZONE", dt.time(9, tzinfo=_UTC), dt.time(12, tzinfo=_PLUS_THREE)),
        (
            "TIMESTAMP(9) WITH TIME ZONE",
            ExactTemporal(1, 0, "UTC"),
            ExactTemporal(1000, 3, "UTC"),
        ),
    ],
)
def test_ip_composite_map_collisions_share_conversion_profile_and_transport_identity(
    sibling_type, first, second
):
    key_type = f"ROW(address IPADDRESS, sibling {sibling_type})"
    native = f"MAP({key_type}, VARCHAR)"
    source = {(_IPV4, first): "first", (_MAPPED, second): "second"}
    original = list(source.items())
    assert len(source) == 2
    resolution = ResolvedColumn(native, target_backend="trino")

    with pytest.raises(ValueError, match="Converted map keys are not unique"):
        resolution.convert(source)
    with pytest.raises(ValueError, match="Converted map keys are not unique"):
        validate_converted_rows([(source,)], [resolution], backend="trino")
    with pytest.raises(ValueError, match="Trino IPADDRESS"):
        validate_ip_transport_value(source, native)

    profile = ColumnProfile(native_type=native, source_backend="trino")
    profile.add(source)
    assert profile.map_duplicate_keys is True
    assert profile.map_comparable_keys is True
    keys = [ResolvedColumn(key_type, target_backend="trino").convert(key) for key in source]
    identities = [profile_map_key_identity(key, key_type, "trino") for key in keys]
    assert identities[0] == identities[1]
    assert ip_key_identity(next(iter(source)), key_type) == identities[0]
    assert list(source.items()) == original


@pytest.mark.parametrize("sdk", ["current", "minimum-0.320"])
@pytest.mark.parametrize(
    "keys",
    [
        ((_IPV4, "a"), (_MAPPED, "b")),
        ((_IPV4, "a"), ("192.0.2.2", "a ")),
        ((_IPV4, "a"), (_MAPPED, "a\u00a0")),
    ],
)
def test_distinct_native_components_keep_payloads_and_supported_sdk_wires(monkeypatch, sdk, keys):
    use_actual_formatter(monkeypatch, sdk)
    native = "MAP(ROW(address IPADDRESS, label CHAR(2)), VARCHAR)"
    source = {keys[0]: "first", keys[1]: "second"}
    original = list(source.items())
    resolution = ResolvedColumn(native, target_backend="trino")

    converted = resolution.convert(source)
    profiles = validate_converted_rows([(source,)], [resolution], backend="trino")
    validate_ip_transport_value(converted, native)
    wire = render_parameter(normalize_value(converted, native))

    assert len(converted) == 2
    assert list(converted.items()) == original
    assert profiles[0].map_duplicate_keys is False
    assert profiles[0].map_comparable_keys is True
    assert "first" in wire
    assert "second" in wire
    assert keys[0][0] in wire
    assert keys[1][0] in wire
    assert list(source.items()) == original


def test_identity_recurses_through_mixed_row_and_array_components():
    native = "ROW(address IPADDRESS, labels ARRAY(ROW(label CHAR(2), rounded REAL)))"
    first = (_IPV4, (("a", float(2**24)),))
    second = (_MAPPED, (("a ", float(2**24 + 1)),))
    assert _key_identity(first, native, "trino") == _key_identity(second, native, "trino")
    assert ip_key_identity(first, native) == ip_key_identity(second, native)


@pytest.mark.parametrize("duplicate", [False, True])
def test_table_row_null_components_keep_null_safe_composite_key_semantics(duplicate):
    native = "ROW(address IPADDRESS, label CHAR(2), note VARCHAR)"
    second_address = _MAPPED if duplicate else "192.0.2.2"
    index = pd.Index(["later", "earlier"], name="input")
    frame = pd.DataFrame(
        {
            "key": pd.Series(
                [(_IPV4, "a", None), (second_address, "a ", None)], index=index, dtype=object
            )
        },
        index=index,
    )
    original = frame.copy(deep=True)
    schema = {"key": native}

    (validation,) = dataframe_key_validation_frames(frame, ["key"], (schema,), "trino")

    pd.testing.assert_index_equal(validation.index, frame.index)
    assert validation["key"].dtype == object
    assert [key[2] for key in validation["key"]] == [None, None]
    if duplicate:
        with pytest.raises(ValueError, match=r"Duplicate.*key"):
            _validate_dataframe_key_uniqueness(validation, ["key"])
    else:
        _validate_dataframe_key_uniqueness(validation, ["key"])
    pd.testing.assert_frame_equal(frame, original)
    assert frame.index is index
    with pytest.raises(ValueError, match="NULL components"):
        ResolvedColumn(f"MAP({native}, VARCHAR)", target_backend="trino").convert(
            {(_IPV4, "a", None): "value"}
        )


def test_shared_key_context_keeps_other_backends_null_rules_and_unsupported_map_keys():
    assert _key_identity((None,), "Tuple(Nullable(Int64))", "ch") == (None,)
    with pytest.raises(ValueError, match="NULL components"):
        _key_identity((None,), "Tuple(Int64)", "ch", allow_null_components=True)
    with pytest.raises(ValueError, match="NULL components"):
        _key_identity((None,), "ROW(v VARCHAR)", "gp", allow_null_components=True)
    with pytest.raises(ValueError, match="unsupported native key structure"):
        _key_identity({}, "MAP(IPADDRESS, VARCHAR)", "trino", allow_null_components=True)


def test_existing_nan_null_and_sdk_parameter_shape_constraints_are_not_relaxed():
    native = "MAP(ROW(address IPADDRESS, value DOUBLE), VARCHAR)"
    with pytest.raises(ValueError, match="NULL components"):
        ResolvedColumn(native, target_backend="trino").convert({(_IPV4, float("nan")): "value"})
    mixed = {(_IPV4, "a"): "first", (bytes.fromhex("c0000202"), "b"): "second"}
    with pytest.raises(ValueError, match="Trino IPADDRESS"):
        validate_ip_transport_value(mixed, "MAP(ROW(address IPADDRESS, label CHAR(2)), VARCHAR)")
    with pytest.raises(ValueError, match="supported hashable representation"):
        ResolvedColumn("MAP(ARRAY(IPADDRESS), VARCHAR)", target_backend="trino").convert(
            {(_IPV4,): "value"}
        )
    assert isinstance(
        converted_map([(1, "a"), (1, "b")], "ch", mapping_input=False), OrderedMapPairs
    )


@pytest.mark.parametrize("character_type", ["CHAR(2)", "CHARACTER(2)"])
def test_time_and_character_effective_keys_compose_without_an_ip_column(character_type):
    clocks = [dt.time(12, tzinfo=_UTC), dt.time(12, tzinfo=_PLUS_THREE)]
    frame = pd.DataFrame({"clock": clocks, "label": ["a", "a "]})
    original = frame.copy(deep=True)
    schema = {"clock": "TIME(12)", "label": character_type}
    keys = ["clock", "label"]
    _validate_dataframe_key_uniqueness(frame, keys)

    (validation,) = dataframe_key_validation_frames(frame, keys, (schema,), "trino")

    assert validation["clock"].dtype == object
    assert all(isinstance(value, int) and value > 2**53 for value in validation["clock"])
    assert validation["clock"].iloc[0] == validation["clock"].iloc[1]
    assert validation["label"].tolist() == ["a", "a"]
    with pytest.raises(ValueError, match=r"Duplicate.*key"):
        _validate_dataframe_key_uniqueness(validation, keys)
    pd.testing.assert_frame_equal(frame, original)
    assert all(value is clock for value, clock in zip(frame["clock"], clocks))


def test_typed_text_views_use_written_values_and_keep_schema_dedup_and_legacy_import():
    frame = pd.DataFrame({"payload": pd.Series([1, "1"], dtype=object), "ignored": [2, 3]})
    original = frame.copy(deep=True)
    schema = {"payload": "VARCHAR"}
    keys = ["payload"]
    _validate_dataframe_key_uniqueness(frame, keys)

    views = list(
        dataframe_key_validation_frames(frame, keys, (None, {}, schema, dict(schema)), "trino")
    )
    legacy = list(legacy_key_validation_frames(frame, keys, (schema,)))

    assert len(views) == 1
    assert len(legacy) == 1
    pd.testing.assert_frame_equal(views[0], legacy[0])
    assert list(views[0].columns) == keys
    assert views[0]["payload"].tolist() == ["1", "1"]
    with pytest.raises(ValueError, match=r"Duplicate.*key"):
        _validate_dataframe_key_uniqueness(views[0], keys)
    assert list(dataframe_key_validation_frames(frame, None, (schema,), "trino")) == []
    assert list(dataframe_key_validation_frames(frame, keys, (None, {}), "trino")) == []
    pd.testing.assert_frame_equal(frame, original)


def test_generic_typed_key_views_do_not_relax_general_null_key_rejection():
    frame = pd.DataFrame({"payload": ["value", None]})
    (validation,) = dataframe_key_validation_frames(
        frame, ["payload"], ({"payload": "VARCHAR"},), "trino"
    )
    assert validation["payload"].tolist() == ["value", None]
    with pytest.raises(ValueError, match=r"Null.*key"):
        _validate_dataframe_key_uniqueness(validation, ["payload"])
