"""Native IPADDRESS cast certification uses pinned server literal semantics."""

from __future__ import annotations

import datetime as dt
import ipaddress
import socket
from enum import Enum

import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends.conversion_profiles import validate_converted_rows
from analytics_toolkit.sql.backends.native_values import (
    dataframe_key_validation_frames,
    validate_transport_value,
)
from analytics_toolkit.sql.backends.trino.ip_values import (
    ip_value_identity,
    is_ip_type,
    validate_ip_profile,
)
from analytics_toolkit.sql.backends.trino.parameters import normalize_value
from analytics_toolkit.sql.backends.type_resolution import (
    ColumnProfile,
    ResolvedColumn,
    validate_profile,
)
from analytics_toolkit.sql.core.exact_values import OrderedMapPairs
from analytics_toolkit.sql.dml.load.load_df import _validate_dataframe_key_uniqueness

from tests.sql._support.trino_binary_buffers import use_actual_formatter
from tests.sql._support.trino_ipaddress_casts import native_ipaddress_target
from tests.sql._support.trino_parameters import render_parameter

MAPPED = b"\x00" * 10 + b"\xff\xff"
V4 = b"\xc0\x00\x02\x01"
V6 = bytes.fromhex("20010db8000000000000000000000001")
_MAP_KEY_ERROR = (
    r"^Selected input cannot preserve its keys "
    r"in the authoritative map schema\.$"
)
_NESTED_SCHEMA_ERROR = r"^Selected input is incompatible with authoritative nested schema\.$"


def certify(value, native="IPADDRESS"):
    profile = ColumnProfile(native_type=native, source_backend="trino", temporal_type=native)
    profile.add(value)
    validate_profile(profile, native, "trino")
    resolution = ResolvedColumn(native, target_backend="trino")
    validate_converted_rows([(value,)], [resolution], backend="trino")
    return normalize_value(resolution.convert(value), native)


class Address(Enum):
    TEXT = "192.0.2.1"
    PACKED = V4


@pytest.mark.parametrize("sdk", ["current", "minimum-0.320"])
@pytest.mark.parametrize(
    ("value", "canonical"),
    [
        ("0.0.0.0", "0.0.0.0"),
        ("255.255.255.255", "255.255.255.255"),
        ("192.0.2.1", "192.0.2.1"),
        ("::", "::"),
        ("::1", "::1"),
        ("2001:db8::1", "2001:db8::1"),
        ("2001:DB8:0:0:0:0:0:1", "2001:db8::1"),
        ("1:2:3:4:5:6:7:8", "1:2:3:4:5:6:7:8"),
        ("::1:2:3:4:5:6:7", "0:1:2:3:4:5:6:7"),
        ("1:2:3:4:5:6:7::", "1:2:3:4:5:6:7:0"),
        ("::ffff:192.0.2.1", "::ffff:c000:201"),
        ("::192.0.2.1", "::c000:201"),
        ("1:2:3:4:5:6:192.0.2.1", "1:2:3:4:5:6:c000:201"),
        ("\uff11\uff19\uff12.\uff11\uff16\uff18.\uff10.\uff11", "192.168.0.1"),
        ("\uff10\uff10\uff11.2.3.4", "1.2.3.4"),
        ("\uff12\uff10\uff10\uff11:\uff24\uff22\uff18::\uff11", "2001:db8::1"),
        ("2001:\uff44\uff42\uff18::1", "2001:db8::1"),
        ("fe80::1%0", "fe80::1"),
        ("fe80::1%2147483647", "fe80::1"),
        ("fe80::1%\uff10\uff12", "fe80::1"),
        ("fe80::1%", "fe80::1"),
    ],
)
def test_ipaddress_casts_preserve_valid_string_wire_and_native_identity(
    monkeypatch, sdk, value, canonical
):
    use_actual_formatter(monkeypatch, sdk)

    converted = certify(value)

    expected = ipaddress.ip_address(canonical).packed
    if len(expected) == 4:
        expected = MAPPED + expected
    assert ip_value_identity(converted) == expected
    assert converted is value
    assert render_parameter(converted) == "'" + value + "'"


@pytest.mark.parametrize("sdk", ["current", "minimum-0.320"])
@pytest.mark.parametrize(
    ("value", "wire", "identity"),
    [
        (V4, "X'c0000201'", MAPPED + V4),
        (V6, "X'20010db8000000000000000000000001'", V6),
        (bytearray(V4), "X'c0000201'", MAPPED + V4),
        (memoryview(V6), "X'20010db8000000000000000000000001'", V6),
        (Address.TEXT, "'192.0.2.1'", MAPPED + V4),
        (Address.PACKED, "X'c0000201'", MAPPED + V4),
    ],
)
def test_ipaddress_casts_preserve_exact_packed_bytes_and_enum_values(
    monkeypatch, sdk, value, wire, identity
):
    use_actual_formatter(monkeypatch, sdk)

    converted = certify(value)

    assert ip_value_identity(converted) == identity
    assert render_parameter(converted) == wire
    if isinstance(value, (bytes, bytearray, memoryview)):
        assert bytes(value) == bytes(converted)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "private.invalid",
        "192.0.2.1 ",
        " 192.0.2.1",
        "192.0.002.1",
        "256.0.2.1",
        "127.1",
        "192.0.2.",
        "+192.0.2.1",
        "192.0.2.a",
        "[2001:db8::1]",
        "192.0.2.1/24",
        "1:2:3",
        "1:2:3:4:5:6:7:8:9",
        "1:2:3:4:5:6:7:8::",
        "1::2::3",
        "12345::1",
        "2001:db8::g",
        ":1:2:3:4:5:6:7",
        "1:2:3:4:5:6:7:",
        ":::1",
        "::ffff:192.0.02.1",
        "192.0.2.1::1",
        "192.0.2.1%1",
        "::ffff:192.0.2.1%1",
        "::ffff:c000:201%1",
        "fe80::1%server-interface",
        "fe80::1%-1",
        "fe80::1%2147483648",
        "fe80::1%1%2",
        "\U0001d7cf.2.3.4",
        "\u00b2.2.3.4",
        "192.0.2.\ud800",
        b"123",
        b"",
        b"x" * 17,
        3221225985,
        ipaddress.IPv4Address("192.0.2.1"),
    ],
)
def test_ipaddress_casts_reject_unsupported_literals_without_dns_or_value_disclosure(
    monkeypatch, value
):
    def network_forbidden(*_args, **_kwargs):
        pytest.fail("IP literal validation must not perform a network lookup")

    monkeypatch.setattr(socket, "getaddrinfo", network_forbidden)
    monkeypatch.setattr(socket, "gethostbyname", network_forbidden)

    with pytest.raises(ValueError, match="authoritative Trino IPADDRESS") as error:
        certify(value)

    assert "private.invalid" not in str(error.value)
    assert "server-interface" not in str(error.value)


@pytest.mark.parametrize("sdk", ["current", "minimum-0.320"])
@pytest.mark.parametrize(
    ("value", "native", "wire"),
    [
        (
            ["192.0.2.1", None, "2001:db8::1"],
            "ARRAY(IPADDRESS)",
            "ARRAY['192.0.2.1',NULL,'2001:db8::1']",
        ),
        ([V4, V6], "ARRAY(IPADDRESS)", "ARRAY[X'c0000201',X'20010db8000000000000000000000001']"),
        (
            {"text": "192.0.2.1", "packed": V4},
            "ROW(text IPADDRESS, packed IPADDRESS)",
            "ROW('192.0.2.1',X'c0000201')",
        ),
        ({"item": "192.0.2.1"}, "MAP(VARCHAR,IPADDRESS)", "MAP(ARRAY['item'], ARRAY['192.0.2.1'])"),
        (
            {"192.0.2.1": 1, "2001:db8::1": 2},
            "MAP(IPADDRESS,BIGINT)",
            "MAP(ARRAY['192.0.2.1','2001:db8::1'], ARRAY[1,2])",
        ),
        (
            {("192.0.2.1", 1): "one", ("192.0.2.2", 2): "two"},
            "MAP(ROW(address IPADDRESS, number BIGINT),VARCHAR)",
            "MAP(ARRAY[ROW('192.0.2.1',1),ROW('192.0.2.2',2)], ARRAY['one','two'])",
        ),
    ],
)
def test_ipaddress_casts_nested_native_parameters_keep_compatible_sql_types(
    monkeypatch, sdk, value, native, wire
):
    use_actual_formatter(monkeypatch, sdk)

    converted = certify(value, native)

    assert render_parameter(converted) == wire


@pytest.mark.parametrize(
    ("value", "native", "expected_error"),
    [
        (["192.0.2.1", V4], "ARRAY(IPADDRESS)", "IPADDRESS"),
        ([["192.0.2.1"], [V4]], "ARRAY(ARRAY(IPADDRESS))", "IPADDRESS"),
        ({"one": "192.0.2.1", "two": V4}, "MAP(VARCHAR,IPADDRESS)", "IPADDRESS"),
        (
            {"192.0.2.1": "one", "::ffff:192.0.2.1": "two"},
            "MAP(IPADDRESS,VARCHAR)",
            _MAP_KEY_ERROR,
        ),
        ({V4: "one", MAPPED + V4: "two"}, "MAP(IPADDRESS,VARCHAR)", _MAP_KEY_ERROR),
        (
            {("192.0.2.1", 1): "one", ("::ffff:192.0.2.1", 1): "two"},
            "MAP(ROW(address IPADDRESS, number BIGINT),VARCHAR)",
            _MAP_KEY_ERROR,
        ),
    ],
)
def test_ipaddress_casts_reject_incompatible_container_wire_and_post_cast_key_collisions(
    value, native, expected_error
):
    with pytest.raises(ValueError, match=expected_error):
        certify(value, native)


@pytest.mark.parametrize("values", [[], [None], [None, None]])
def test_ipaddress_casts_empty_and_all_null_columns_retain_the_authoritative_native_type(values):
    profiles = validate_converted_rows(
        [(value,) for value in values],
        [ResolvedColumn("IPADDRESS", target_backend="trino")],
        backend="trino",
    )

    assert profiles[0].count == len(values)
    assert profiles[0].null_count == len(values)


def test_ipaddress_casts_each_row_may_use_an_independent_supported_native_parameter_type():
    profiles = validate_converted_rows(
        [("192.0.2.1",), (V4,), (None,)],
        [ResolvedColumn("IPADDRESS", target_backend="trino")],
        backend="trino",
    )

    assert profiles[0].families == {"string", "binary"}
    assert profiles[0].null_count == 1


@pytest.mark.parametrize("sdk", ["current", "minimum-0.320"])
def test_ipaddress_casts_native_ordered_row_map_keys_keep_field_binding_and_wire(monkeypatch, sdk):
    use_actual_formatter(monkeypatch, sdk)
    key = {"number": 1, "address": "192.0.2.1"}
    value = OrderedMapPairs(((key, "one"),))
    native = "MAP(ROW(address IPADDRESS, number BIGINT),VARCHAR)"

    validate_transport_value(value, native, "trino")
    converted = normalize_value(value, native)

    assert render_parameter(converted) == "MAP(ARRAY[ROW('192.0.2.1',1)], ARRAY['one'])"
    assert value.pairs[0][0] is key
    assert key == {"number": 1, "address": "192.0.2.1"}


@pytest.mark.parametrize(
    ("value", "native", "expected_error"),
    [
        ({None: "one"}, "MAP(IPADDRESS,VARCHAR)", r"IPADDRESS|array|ARRAY|authoritative map"),
        ({("192.0.2.1",): "one"}, "MAP(ARRAY(IPADDRESS),VARCHAR)", _NESTED_SCHEMA_ERROR),
        (
            [("192.0.2.1",), ("192.0.2.2", "label")],
            "ARRAY(ROW(address IPADDRESS,label VARCHAR))",
            r"IPADDRESS|array|ARRAY|authoritative map",
        ),
    ],
)
def test_ipaddress_casts_transport_rejects_invalid_native_key_and_row_constructors(
    value, native, expected_error
):
    with pytest.raises(ValueError, match=expected_error):
        validate_transport_value(value, native, "trino")


@pytest.mark.parametrize("native", ["IPADDRESS(4)", "ARRAY(IPADDRESS(16))"])
@pytest.mark.parametrize("value", [None, []])
def test_ipaddress_casts_reject_invalid_type_parameters_even_without_non_null_values(native, value):
    with pytest.raises(ValueError, match="does not accept type arguments"):
        certify(value, native)


@pytest.mark.parametrize("native", ["VARCHAR", "IPPREFIX", '"IPADDRESS"', "custom.IPADDRESS"])
def test_ipaddress_casts_do_not_certify_unrelated_or_custom_native_types(native):
    assert not is_ip_type(native)
    assert not validate_ip_profile(ColumnProfile(), native)
    validate_transport_value(None, native, "trino")


def test_ipaddress_casts_explicit_dataframe_dry_run_never_opens_or_mutates_a_target(monkeypatch):
    database = native_ipaddress_target(monkeypatch)
    frame = pd.DataFrame({"payload": pd.Series(["192.0.2.1", None], dtype=object)})

    plan = sql.load_df(
        "trino",
        "iceberg.sandbox.target",
        frame,
        table_schema={"payload": "IPADDRESS"},
        write_mode="truncate_insert",
        dry_run=True,
    )

    assert plan.options["table_schema"] == {"payload": "IPADDRESS"}
    assert database.opened == []
    assert database.statements == []
    assert database.mutations == []


@pytest.mark.parametrize(
    "different_partition", [False, True], ids=["same-partition", "different-partitions"]
)
@pytest.mark.parametrize("schema_source", ["existing", "explicit", "both"])
def test_ipaddress_casts_dataframe_effective_keys_follow_declared_native_partition_identity(
    different_partition, schema_source
):
    frame = pd.DataFrame(
        {
            "month": pd.Series(
                ["January", "February" if different_partition else "January"], dtype="string"
            ),
            "payload": pd.Series(["192.0.2.1", "::ffff:192.0.2.1"], dtype="string"),
            "other": [1, 2],
        }
    )
    frame.index = [4, 7]
    original = frame.copy(deep=True)
    keys = ["month", "payload"]
    ip_schema = {"month": "VARCHAR", "payload": "IPADDRESS"}
    text_schema = {"month": "VARCHAR", "payload": "VARCHAR"}
    schemas = (
        ip_schema if schema_source in {"existing", "both"} else text_schema,
        ip_schema if schema_source in {"explicit", "both"} else None,
    )
    _validate_dataframe_key_uniqueness(frame, keys)

    frames = list(dataframe_key_validation_frames(frame, keys, schemas, "trino"))

    assert len(frames) == (2 if schema_source == "explicit" else 1)
    if schema_source == "explicit":
        pd.testing.assert_frame_equal(frames[0], frame.loc[:, keys])
        _validate_dataframe_key_uniqueness(frames[0], keys)
    validation = frames[-1]
    assert list(validation.columns) == keys
    assert validation["payload"].dtype == object
    assert validation["payload"].tolist() == [MAPPED + V4, MAPPED + V4]
    pd.testing.assert_series_equal(validation["month"], frame["month"])
    if different_partition:
        _validate_dataframe_key_uniqueness(validation, keys)
    else:
        with pytest.raises(ValueError, match=r"Duplicate.*key"):
            _validate_dataframe_key_uniqueness(validation, keys)
    pd.testing.assert_frame_equal(frame, original)


@pytest.mark.parametrize("backend", ["gp", "ch", "trino"])
@pytest.mark.parametrize("key_columns", [None, ["payload"]])
def test_ipaddress_casts_key_dispatch_never_guesses_types_or_changes_keyless_append(
    backend, key_columns
):
    frame = pd.DataFrame({"payload": ["192.0.2.1", "::ffff:192.0.2.1"]})
    schemas = ({"payload": "IPADDRESS"},) if backend != "trino" else (None, {"payload": "VARCHAR"})

    frames = list(dataframe_key_validation_frames(frame, key_columns, schemas, backend))
    if backend in {"trino", "ch"} and key_columns:
        assert len(frames) == 1
        pd.testing.assert_frame_equal(frames[0], frame)
        assert frames[0]["payload"].tolist() == ["192.0.2.1", "::ffff:192.0.2.1"]
        _validate_dataframe_key_uniqueness(frames[0], key_columns)
    else:
        assert frames == []


@pytest.mark.parametrize("sdk", ["current", "minimum-0.320"])
@pytest.mark.parametrize(
    ("native", "same_wall", "duplicate"),
    [
        ("TIME(6)", False, False),
        ("TIME(6)", True, True),
        ("TIME(6) WITH TIME ZONE", False, True),
        ("TIME(6) WITH TIME ZONE", True, False),
    ],
)
def test_ipaddress_casts_native_clock_keys_follow_the_written_clock_domain(
    monkeypatch, sdk, native, same_wall, duplicate
):
    use_actual_formatter(monkeypatch, sdk)
    clocks = [
        dt.time(12, tzinfo=dt.timezone(dt.timedelta(hours=3))),
        dt.time(12 if same_wall else 9, tzinfo=dt.timezone.utc),
    ]
    frame = pd.DataFrame({"payload": ["192.0.2.1", "::ffff:192.0.2.1"], "clock": clocks})
    keys = ["payload", "clock"]
    schema = {"payload": "IPADDRESS", "clock": native}
    wires = [render_parameter(normalize_value(clock, native)) for clock in clocks]
    expected = ["'12:00:00'", "'12:00:00'" if same_wall else "'09:00:00'"]
    if "WITH TIME ZONE" in native:
        expected = ["'12:00:00+03:00'", "'12:00:00+00:00'" if same_wall else "'09:00:00+00:00'"]
    assert wires == expected

    (validation,) = dataframe_key_validation_frames(frame, keys, (schema,), "trino")

    if duplicate:
        with pytest.raises(ValueError, match=r"Duplicate.*key"):
            _validate_dataframe_key_uniqueness(validation, keys)
    else:
        _validate_dataframe_key_uniqueness(validation, keys)
    assert all(value is original for value, original in zip(frame["clock"], clocks))


def test_ipaddress_casts_native_clock_views_keep_both_final_and_stage_equality_domains():
    clocks = [
        dt.time(12, tzinfo=dt.timezone(dt.timedelta(hours=3))),
        dt.time(9, tzinfo=dt.timezone.utc),
    ]
    frame = pd.DataFrame({"payload": ["192.0.2.1", "::ffff:192.0.2.1"], "clock": clocks})
    keys = ["payload", "clock"]
    schemas = (
        {"payload": "IPADDRESS", "clock": "TIME(12)"},
        {"payload": "IPADDRESS", "clock": "TIME(12) WITH TIME ZONE"},
    )

    frames = list(dataframe_key_validation_frames(frame, keys, schemas, "trino"))

    assert len(frames) == 2
    assert all(view["clock"].dtype == object for view in frames)
    assert all(
        isinstance(value, int) and value > 2**53 for view in frames for value in view["clock"]
    )
    _validate_dataframe_key_uniqueness(frames[0], keys)
    with pytest.raises(ValueError, match=r"Duplicate.*key"):
        _validate_dataframe_key_uniqueness(frames[1], keys)
