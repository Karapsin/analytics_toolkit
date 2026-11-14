from __future__ import annotations

import json
import struct
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from uuid import UUID

import pandas as pd
import pytest
from analytics_toolkit.sql.backends.ch import client_validation as validation
from analytics_toolkit.sql.backends.ch.insert import normalize_typed_row
from analytics_toolkit.sql.core.exact_values import ExactTemporal

from tests.sql.backends.ch._support.client_validation import client
from tests.sql.backends.ch._support.exact_transport import BinaryNativeSource, native_context


@pytest.fixture
def emitted_bytes(monkeypatch):
    # Instrument the actual SDK encoder.
    from clickhouse_connect.datatypes.base import (  # noqa: PLC0415
        ClickHouseType,
    )

    output = bytearray()

    def sendall(_socket, payload):
        output.extend(payload)

    original = ClickHouseType.write_column

    def write_column(datatype, column, destination, context):
        offset = len(destination)
        original(datatype, column, destination, context)
        output.extend(destination[offset:])

    monkeypatch.setattr(validation._DiscardSocket, "sendall", sendall)
    monkeypatch.setattr(ClickHouseType, "write_column", write_column)
    return output


@pytest.mark.parametrize("transport", ["native", "http"])
@pytest.mark.parametrize(
    ("native", "value", "expected"),
    [
        ("SimpleAggregateFunction(sum, UInt64)", 7, struct.pack("<Q", 7)),
        ("Point", (1.25, 2.5), struct.pack("<2d", 1.25, 2.5)),
        (
            "Nested(x UInt64, y String)",
            [(7, "a")],
            struct.pack("<2Q", 1, 7) + b"\x01a",
        ),
    ],
)
def test_client_validation_certifies_actual_alias_binary(
    transport, native, value, expected, emitted_bytes
) -> None:
    # Minimum HTTP lacks geometry factories. A capability error must precede values.
    if transport == "http" and native == "Point":
        # Check the installed SDK's actual capability.
        from clickhouse_connect.datatypes.registry import (  # noqa: PLC0415
            get_from_name,
        )

        try:
            get_from_name(native)
        except Exception:  # noqa: BLE001 -- old and new SDKs use different type errors.
            with pytest.raises(ValueError, match="actual writer schema"):
                validation.validate_client_write_capabilities(
                    client(transport), {"value": native}, rows=[(value,)]
                )
            assert not emitted_bytes
            return
    original = deepcopy(value)
    certificate = validation.validate_client_write_capabilities(
        client(transport), {"value": native}, rows=[(value,)]
    )
    certificate.require_complete(1)
    assert certificate.driver == transport
    assert certificate.column_types == (("value", native),)
    assert certificate.encoded_columns == ("value",)
    assert certificate.certified_input_count == 1
    assert certificate.validation_basis == "encoded-input"
    assert bytes(emitted_bytes) == expected
    assert value == original


def test_native_legacy_json_retains_objects_raw_text_and_actual_encoder(emitted_bytes) -> None:
    from clickhouse_driver.reader import (  # noqa: PLC0415 -- real binary framing.
        read_binary_str,
        read_binary_uint8,
    )

    text = ' { "a": 1, "a": 2 } '
    rows = [({"a": [1, None], "ok": True},), (text,)]
    original = deepcopy(rows)
    certificate = validation.validate_client_write_capabilities(
        client("native"), {"payload": "Object('json')"}, rows=iter(rows)
    )
    certificate.require_complete(2)
    assert certificate.opaque_columns == ("payload",)
    assert certificate.certified_input_count == 2
    assert rows == original
    source = BinaryNativeSource(emitted_bytes)
    assert read_binary_uint8(source) == 1
    assert json.loads(read_binary_str(source)) == rows[0][0]
    assert read_binary_uint8(source) == 1
    assert read_binary_str(source) == text
    assert not source.read()


def test_http_legacy_json_uses_only_the_installed_factory() -> None:
    # Check the installed SDK's actual Object capability.
    from clickhouse_connect.datatypes.registry import (  # noqa: PLC0415
        get_from_name,
    )

    try:
        get_from_name("Object('json')")
    except Exception:  # noqa: BLE001 -- supported SDK generations expose different errors.
        with pytest.raises(ValueError, match="actual writer schema"):
            validation.validate_client_write_capabilities(
                client("http"), {"payload": "Object('json')"}, rows=[({"a": 1},)]
            )
        return
    certificate = validation.validate_client_write_capabilities(
        client("http"), {"payload": "Object('json')"}, rows=[({"a": 1},), ({"a": 2},)]
    )
    certificate.require_complete(2)
    mixed = validation.validate_client_write_capabilities(
        client("http"), {"payload": "Object('json')"}, rows=[({"a": 1},), ('{"a":2}',)]
    )
    mixed.require_complete(2)


def test_client_validation_canonicalizes_http_alias_first_format_mixture(emitted_bytes) -> None:
    # Compare against the actual whole-block codec and its batch context.
    from clickhouse_connect.datatypes.registry import (  # noqa: PLC0415
        get_from_name,
    )
    from clickhouse_connect.driver.insert import (  # noqa: PLC0415
        InsertContext,
    )

    spec = "SimpleAggregateFunction(any, UUID)"
    rows = [(UUID(int=1),), (str(UUID(int=2)),)]
    certificate = validation.validate_client_write_capabilities(
        client("http"), {"value": spec}, rows=rows
    )
    certificate.require_complete(2)
    expected = struct.pack("<4Q", 0, 1, 0, 2)
    assert bytes(emitted_bytes) == expected
    datatype = get_from_name(spec)
    context = InsertContext("db.target", ["value"], [datatype])
    context.start_column("value")
    prepared = [
        normalize_typed_row(["value"], row, {"value": spec}, native_transport=False)[0]
        for row in rows
    ]
    output = bytearray()
    datatype.write_column(prepared, output, context)
    assert bytes(output) == expected


@pytest.mark.parametrize("transport", ["native", "http"])
def test_client_validation_mixed_schema_encodes_only_alias_and_keeps_wide_integer_companion(
    transport, emitted_bytes
) -> None:
    schema = {"id": "UInt64", "amount": "SimpleAggregateFunction(sum, UInt64)"}
    rows = [(2**64 - 1, 7), (2**53 + 1, 8)]
    original = list(rows)

    certificate = validation.validate_client_write_capabilities(
        client(transport), schema, batches=[rows]
    )

    certificate.require_complete(2)
    assert certificate.column_types == tuple(schema.items())
    assert certificate.encoded_columns == ("amount",)
    assert certificate.certified_input_count == 2
    assert bytes(emitted_bytes) == struct.pack("<2Q", 7, 8)
    assert rows == original


@pytest.mark.parametrize("transport", ["http", "native"])
def test_client_validation_rejects_nonnullable_null_before_sdk_default_fill(transport) -> None:
    with pytest.raises(ValueError, match="Selected input"):
        validation.validate_client_write_capabilities(
            client(transport),
            {"value": "SimpleAggregateFunction(any, UUID)"},
            rows=[(None,), (UUID(int=1),)],
        )


@pytest.mark.parametrize("transport", ["http", "native"])
def test_client_validation_encodes_ordinary_normalization_physical_mismatch(transport) -> None:
    with pytest.raises(ValueError, match="Selected input"):
        validation.validate_client_write_capabilities(
            client(transport),
            {"value": "String"},
            normalization_schema={"value": "UUID"},
            batches=[[(UUID(int=1),)]],
        )


@pytest.mark.parametrize("transport", ["http", "native"])
def test_client_validation_retains_nullable_uuid_and_json_internal_null(transport) -> None:
    certificate = validation.validate_client_write_capabilities(
        client(transport),
        {"value": "SimpleAggregateFunction(any, Nullable(UUID))"},
        batches=[[(None,), (UUID(int=1),)]],
    )
    certificate.require_complete(2)
    opaque = validation.validate_client_write_capabilities(
        client("native"), {"payload": "Object('json')"}, batches=[[({"a": None},)]]
    )
    opaque.require_complete(1)


@pytest.mark.parametrize("transport", ["http", "native"])
def test_client_validation_preserves_nested_nanosecond_ticks(transport, emitted_bytes) -> None:
    ticks = 1_700_000_000_123456789
    native = "Nested(stamp DateTime64(9, 'UTC'), n UInt64)"
    value = [(ExactTemporal(ticks, 9, "UTC"), 7)]
    certificate = validation.validate_client_write_capabilities(
        client(transport), {"value": native}, rows=[(value,)]
    )
    certificate.require_complete(1)
    assert bytes(emitted_bytes) == struct.pack("<QqQ", 1, ticks, 7)


@pytest.mark.parametrize("block_size", [1, 2])
def test_client_validation_uses_native_publication_block_geometry(
    block_size, emitted_bytes
) -> None:
    from clickhouse_driver.reader import (  # noqa: PLC0415 -- actual codec framing.
        read_binary_str,
        read_binary_uint8,
    )

    connection = client("native")
    connection.context.client_settings = {
        **connection.context.client_settings,
        "insert_block_size": block_size,
    }
    rows = [({"a": 1},), (' {"a":2} ',)]
    certificate = validation.validate_client_write_capabilities(
        connection, {"payload": "Object('json')"}, batches=[rows]
    )
    certificate.require_complete(2)
    assert certificate.geometry_basis == "publication-batches"
    assert len(certificate.geometry_digest) == 64
    source = BinaryNativeSource(emitted_bytes)
    assert read_binary_uint8(source) == 1
    assert json.loads(read_binary_str(source)) == {"a": 1}
    if block_size == 1:
        assert read_binary_uint8(source) == 1
    assert read_binary_str(source) == rows[1][0]
    assert not source.read()


def test_client_validation_distinguishes_outer_and_internal_native_batches() -> None:
    connection = client("native")
    schema = {"value": "SimpleAggregateFunction(sum, UInt64)"}
    single = validation.validate_client_write_capabilities(
        connection, schema, batches=[[(1,), (2,)]]
    )
    separate = validation.validate_client_write_capabilities(
        connection, schema, batches=[[(1,)], [(2,)]]
    )
    connection.context.client_settings = {
        **connection.context.client_settings,
        "insert_block_size": 1,
    }
    internal = validation.validate_client_write_capabilities(
        connection, schema, batches=[[(1,), (2,)]]
    )
    for certificate in (single, separate, internal):
        certificate.require_complete(2)
    assert len({item.geometry_digest for item in (single, separate, internal)}) == 3


def test_native_validation_uses_next_query_base_settings_not_previous_context(
    emitted_bytes,
) -> None:
    # Construct the actual SDK client without establishing a connection.
    from clickhouse_driver import (  # noqa: PLC0415
        Client,
    )
    from clickhouse_driver.reader import (  # noqa: PLC0415 -- actual block framing.
        read_binary_str,
        read_binary_uint8,
    )

    raw = Client("not-connected.invalid", settings={"insert_block_size": 2})
    context = raw.connection.context
    context.server_info = native_context().server_info
    stale_settings = {**raw.client_settings, "insert_block_size": 1, "strings_as_bytes": True}
    context.client_settings = stale_settings
    connection = SimpleNamespace(is_native_transport=True, _client=raw)
    certificate = validation.validate_client_write_capabilities(
        connection,
        {"payload": "Object('json')"},
        batches=[[({"a": 1},), ({"a": 2},)]],
    )
    certificate.require_complete(2)
    source = BinaryNativeSource(emitted_bytes)
    assert read_binary_uint8(source) == 1
    assert json.loads(read_binary_str(source)) == {"a": 1}
    assert json.loads(read_binary_str(source)) == {"a": 2}
    assert not source.read()
    assert context.client_settings == stale_settings
    assert raw.client_settings["insert_block_size"] == 2


@pytest.mark.parametrize("transport", ["http", "native"])
def test_client_validation_uses_owned_dataframe_and_actual_publication_normalization(
    transport, emitted_bytes
) -> None:
    ticks = 1_700_000_000_123456789
    spec = "Nested(stamp DateTime64(9, 'UTC'), n UInt64)"
    value = [(ExactTemporal(ticks, 9, "UTC"), 7)]
    frame = pd.DataFrame({"value": pd.Series([value], dtype=object)})
    certificate = validation.validate_client_write_capabilities(
        client(transport), {"value": spec}, batches=[frame]
    )
    certificate.require_complete(1)
    assert certificate.geometry_basis == "publication-batches"
    assert frame.iloc[0, 0] == value
    assert bytes(emitted_bytes) == struct.pack("<QqQ", 1, ticks, 7)


def test_factory_only_validation_does_not_consume_batches() -> None:
    def batches():
        pytest.fail("declared ordinary types retain lazy streaming")
        yield [(7,)]

    certificate = validation.validate_client_write_capabilities(
        client("native", context=False), {"value": "UInt64"}, batches=batches()
    )
    assert certificate.certified_input_count is None
    assert certificate.geometry_basis == "type-only"
    assert certificate.geometry_digest is None


def test_rows_and_publication_batches_are_mutually_exclusive() -> None:
    with pytest.raises(ValueError, match="not both") as caught:
        validation.validate_client_write_capabilities(
            client("native"), {"value": "UInt64"}, rows=[], batches=[]
        )
    assert caught.value.analytics_toolkit_sql_retry_safe is False


@pytest.mark.parametrize("transport", ["http", "native"])
@pytest.mark.parametrize(
    ("value", "wire"),
    [
        (memoryview(b"abc")[::2], b"\x02ac"),
        (memoryview(b"\x01\x02\x03\x04").cast("H"), b"\x04\x01\x02\x03\x04"),
    ],
)
def test_client_validation_copies_buffers_without_pickle(
    transport, value, wire, emitted_bytes
) -> None:
    certificate = validation.validate_client_write_capabilities(
        client(transport), {"value": "SimpleAggregateFunction(any, String)"}, rows=[(value,)]
    )
    certificate.require_complete(1)
    assert bytes(emitted_bytes) == wire


@pytest.mark.parametrize("transport", ["http", "native"])
def test_unsupported_selected_codec_never_consumes_input(transport) -> None:
    def rows():
        pytest.fail("type support must be established before consuming values")
        yield ()

    with pytest.raises(ValueError, match="actual writer schema"):
        validation.validate_client_write_capabilities(
            client(transport), {"payload": "UnsupportedToolkitCodec"}, rows=rows()
        )


@pytest.mark.parametrize(
    "native",
    ["AggregateFunction(sum, UInt64)", "Array(AggregateFunction(sum, UInt64))"],
)
def test_http_registered_readonly_types_are_not_writer_capabilities(native) -> None:
    # Confirm the selected SDK factory exists before rejecting its writer.
    from clickhouse_connect.datatypes.registry import (  # noqa: PLC0415
        get_from_name,
    )

    assert get_from_name(native) is not None
    with pytest.raises(ValueError, match="actual writer schema"):
        validation.validate_client_write_capabilities(client("http"), {"value": native})


@pytest.mark.parametrize(
    "native",
    [
        "UnsupportedToolkitCodec('private-type-value')",
        "Array(UnsupportedToolkitCodec('private-type-value'))",
    ],
)
def test_http_unsupported_type_literals_are_not_logged(native, caplog) -> None:
    with pytest.raises(ValueError, match="actual writer schema") as caught:
        validation.validate_client_write_capabilities(client("http"), {"value": native})
    assert "private-type-value" not in str(caught.value)
    assert "private-type-value" not in caplog.text
    assert caught.value.analytics_toolkit_sql_retry_safe is False


def test_alias_certification_requires_real_context_and_complete_count() -> None:
    certificate = validation.validate_client_write_capabilities(
        client("native", context=False), {"value": "SimpleAggregateFunction(sum, UInt64)"}
    )
    assert certificate.certified_input_count is None
    with pytest.raises(ValueError, match="complete frozen input"):
        certificate.require_complete(1)
    with pytest.raises(ValueError, match="active connection context"):
        validation.validate_client_write_capabilities(
            client("native", context=False),
            {"value": "SimpleAggregateFunction(sum, UInt64)"},
            rows=[(7,)],
        )
    empty = validation.validate_client_write_capabilities(
        client("native", context=False),
        {"value": "SimpleAggregateFunction(sum, UInt64)"},
        rows=[],
    )
    empty.require_complete(0)
    assert empty.certified_input_count == 0


def test_declared_domain_proof_preserves_unknown_count_and_rejects_narrowing() -> None:
    certificate = validation.validate_client_write_capabilities(
        client("native", context=False), {"value": "SimpleAggregateFunction(sum, UInt64)"}
    )
    proof = certificate.prove_declared_domain({"value": "UInt32"})
    proof.require_complete(99)
    assert proof.certified_input_count is None
    assert proof.validation_basis == "declared-source-domain"
    assert certificate.validation_basis == "type-capability"
    forged = replace(certificate, validation_basis="declared-source-domain")
    with pytest.raises(ValueError, match="complete frozen input"):
        forged.require_complete(99)
    with pytest.raises(ValueError, match="complete frozen input"):
        certificate.prove_declared_domain(
            {"value": "UInt64"}, intermediate_schema={"value": "UInt8"}
        )
    with pytest.raises(ValueError, match="complete frozen input"):
        certificate.prove_declared_domain({})
    opaque = validation.validate_client_write_capabilities(
        client("native", context=False), {"payload": "Object('json')"}
    )
    with pytest.raises(ValueError, match="complete frozen input"):
        opaque.prove_declared_domain({"payload": "Object('json')"})


def test_declared_domain_proof_does_not_invent_uuid_to_physical_string_conversion() -> None:
    certificate = validation.validate_client_write_capabilities(
        client("http"),
        {"value": "String"},
        normalization_schema={"value": "UUID"},
    )
    with pytest.raises(ValueError, match="complete frozen input"):
        certificate.prove_declared_domain({"value": "UUID"})


def test_declared_domain_proof_preserves_exact_integer_to_decimal_transport() -> None:
    certificate = validation.validate_client_write_capabilities(
        client("native", context=False),
        {"value": "Decimal(10, 0)"},
        normalization_schema={"value": "UInt32"},
    )
    proof = certificate.prove_declared_domain({"value": "UInt32"})
    proof.require_complete(123)
    assert proof.certified_input_count is None
    assert proof.validation_basis == "declared-source-domain"


def test_declared_domain_proof_retains_raw_http_nested_serializer_boundary() -> None:
    incoming = {"value": "Nested(x UInt64)"}
    physical = {"value": "Array(Tuple(x UInt64))"}
    http = validation.validate_client_write_capabilities(
        client("http"), physical, normalization_schema=incoming
    )
    with pytest.raises(ValueError, match="complete frozen input"):
        http.prove_declared_domain(incoming)
    native = validation.validate_client_write_capabilities(
        client("native", context=False), physical, normalization_schema=incoming
    )
    native.prove_declared_domain(incoming).require_complete(123)


def test_native_context_is_found_under_real_wrapper_layouts() -> None:
    context = native_context()
    raw = SimpleNamespace(connection=SimpleNamespace(context=context))
    observed = SimpleNamespace(
        is_native_transport=True,
        _target=SimpleNamespace(_client=SimpleNamespace(_client=raw)),
    )
    certificate = validation.validate_client_write_capabilities(
        observed, {"value": "SimpleAggregateFunction(sum, UInt64)"}, rows=[(7,)]
    )
    certificate.require_complete(1)
    assert validation._active_native_context(observed) is context
    cycle = SimpleNamespace(is_native_transport=True)
    cycle._client = cycle
    assert validation._active_native_context(cycle) is None


@pytest.mark.parametrize(
    "value",
    [
        {"a": [1, None, "é"]},
        '{"a":1,"a":2}',
        "{}",
    ],
)
def test_legacy_json_profile_validation_does_not_rewrite_valid_payload(value) -> None:
    original = deepcopy(value)
    assert validation.is_legacy_json_type("Object('json')")
    validation.validate_legacy_json_value(value, "Object('json')")
    assert value == original


def test_legacy_json_retains_existing_missing_value_normalization() -> None:
    value = {"a": [float("nan"), {"b": float("nan")}], "text": "NaN"}
    result = validation.normalize_legacy_json_value(value, "Object('json')")
    assert result == {"a": [None, {"b": None}], "text": "NaN"}
    assert result is not value
    assert result["a"] is not value["a"]
    assert value["a"][0] != value["a"][0]


@pytest.mark.parametrize(
    "value",
    [
        "[]",
        "null",
        '{"bad":NaN}',
        '{"bad":1e400}',
        '{"bad":"\\ud800","bad":"ok"}',
        {"bad": "\ud800"},
        {"private-invalid-value": object()},
    ],
)
def test_legacy_json_rejects_invalid_root_or_unicode_without_private_errors(value) -> None:
    with pytest.raises(ValueError, match="Object") as caught:
        validation.validate_legacy_json_value(value, "Object('json')")
    assert "private-invalid-value" not in str(caught.value)


def test_only_exact_legacy_object_spec_receives_the_object_policy() -> None:
    assert not validation.is_legacy_json_type("JSON")
    assert not validation.is_legacy_json_type("Object('xml')")
    assert not validation.is_legacy_json_type("Object('json', UInt64)")
    with pytest.raises(ValueError, match="Object"):
        validation.validate_legacy_json_value({"a": 1}, "JSON")


def test_selected_row_headers_and_physical_sample_block_types_stay_distinct() -> None:
    incoming = {"payload": "ipv4", "amount": "SimpleAggregateFunction(sum, UInt64)"}
    actual = {"payload": "IPv4", "amount": "UInt64", "generated": "String"}
    assert validation.effective_client_write_schema(client("native"), incoming, actual) == {
        "payload": "IPv4",
        "amount": "UInt64",
    }
    assert validation.effective_client_write_schema(client("http"), incoming, actual) == {
        "payload": "IPv4",
        "amount": "UInt64",
    }
    assert (
        validation.effective_client_write_schema(
            client("http"), incoming, actual, writer_kind="rows"
        )
        == incoming
    )
    assert validation.effective_client_write_schema(
        client("native"), incoming, actual, writer_kind="rows"
    ) == {"payload": "IPv4", "amount": "UInt64"}
