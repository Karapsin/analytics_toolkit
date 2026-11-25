from __future__ import annotations

import struct
from io import BytesIO
from ipaddress import IPv4Address, IPv6Address
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pytest
from analytics_toolkit.sql.backends.ch.http_codec import ExactHTTPClient
from analytics_toolkit.sql.backends.ch.insert import insert_dataframe_batch
from analytics_toolkit.sql.backends.ch.native_codec import exact_block_stream
from analytics_toolkit.sql.core.exact_values import OrderedMapPairs

from tests.sql.backends.ch._support.network_transport import encode_normalized
from tests.sql.backends.ch.temporal_source_mode import HTTPBinary, NativeBinary


class _CodecEndpoint:
    def __init__(self, spec: str, transport: str) -> None:
        self.spec = spec
        self.transport = transport
        self.is_native_transport = transport == "native"
        self.emitted: list[Any] = []
        self.payload = b""

    def insert(self, **kwargs: Any) -> None:
        assert kwargs["column_names"] == ["address"]
        assert kwargs["column_type_names"] == [self.spec]
        self._encode([row[0] for row in kwargs["data"]])

    def insert_df(self, **kwargs: Any) -> None:
        assert kwargs["column_names"] == ["address"]
        assert kwargs["df"].dtypes.tolist() == [object]
        self._encode(kwargs["df"].iloc[:, 0].tolist())

    def _encode(self, values: list[Any]) -> None:
        self.emitted = values
        self.payload = encode_normalized(self.spec, values, self.transport)


class _NetworkNativeBinary(NativeBinary):
    def block(self) -> Any:
        from clickhouse_driver.bufferedreader import BufferedSocketReader  # noqa: PLC0415

        source = BytesIO(self.binary)
        reader = BufferedSocketReader(SimpleNamespace(recv_into=source.readinto), 256)
        return exact_block_stream(reader, self.raw.connection.context).read()


def _cases() -> list[Any]:
    v4 = IPv4Address("192.0.2.1")
    v6 = IPv6Address("2001:db8::1")
    maximum_v4 = IPv4Address(2**32 - 1)
    maximum_v6 = IPv6Address(2**128 - 1)
    return [
        pytest.param(
            "Nullable(IPv4)",
            [None, str(v4), v4, int(maximum_v4), v4.packed, None],
            [None, v4, v4, maximum_v4, v4, None],
            b"\x01\x00\x00\x00\x00\x01"
            + struct.pack("<6I", 0, int(v4), int(v4), int(maximum_v4), int(v4), 0),
            id="nullable-ipv4-mixed-native-inputs",
        ),
        pytest.param(
            "Nullable(IPv6)",
            [None, str(v6), v6, 1, maximum_v6.packed, None],
            [None, v6, v6, IPv6Address(1), maximum_v6, None],
            b"\x01\x00\x00\x00\x00\x01"
            + bytes(16)
            + v6.packed * 2
            + IPv6Address(1).packed
            + maximum_v6.packed
            + bytes(16),
            id="nullable-ipv6-preserves-integer-family",
        ),
        pytest.param(
            "Array(Nullable(IPv4))",
            [[None, str(v4)], [int(maximum_v4), v4.packed], []],
            [[None, v4], [maximum_v4, v4], []],
            struct.pack("<3Q", 2, 4, 4)
            + b"\x01\x00\x00\x00"
            + struct.pack("<4I", 0, int(v4), int(maximum_v4), int(v4)),
            id="array-nullable-ipv4",
        ),
        pytest.param(
            "Tuple(IPv4, Nullable(IPv6))",
            [(str(v4), None), (int(maximum_v4), 1)],
            [(v4, None), (maximum_v4, IPv6Address(1))],
            struct.pack("<2I", int(v4), int(maximum_v4))
            + b"\x01\x00"
            + bytes(16)
            + IPv6Address(1).packed,
            id="tuple-ipv4-nullable-ipv6",
        ),
        pytest.param(
            "Map(IPv4, IPv6)",
            [OrderedMapPairs(((str(v4), str(v6)),)), {int(maximum_v4): 1}],
            [OrderedMapPairs(((v4, v6),)), {maximum_v4: IPv6Address(1)}],
            struct.pack("<2Q2I", 1, 2, int(v4), int(maximum_v4))
            + v6.packed
            + IPv6Address(1).packed,
            id="map-ipv4-keys-ipv6-values",
        ),
    ]


def _mapped_case() -> Any:
    address = IPv4Address("192.0.2.1")
    mapped = IPv6Address("::ffff:192.0.2.1")
    # Older supported HTTP readers expose mapped IPv6 as IPv4Address, while
    # both driver generations accept IPv6Address and write the same 128 bits.
    return pytest.param(
        "IPv6",
        [address, str(address), address.packed, mapped, mapped.packed, "::ffff:192.0.2.1"],
        [mapped] * 6,
        mapped.packed * 6,
        id="ipv4-forms-map-into-ipv6",
    )


@pytest.mark.parametrize("transport", ["native", "http"])
@pytest.mark.parametrize(("spec", "values", "canonical", "payload"), [*_cases(), _mapped_case()])
def test_network_adapter_emits_exact_native_values_and_real_binary(
    transport: str,
    spec: str,
    values: list[Any],
    canonical: list[Any],
    payload: bytes,
) -> None:
    endpoint = _CodecEndpoint(spec, transport)
    frame = pd.DataFrame({"address": pd.Series(values, dtype=object)})
    progress: list[int] = []
    insert_dataframe_batch(endpoint, "db.target", frame, {"address": spec}, progress.append)
    assert endpoint.emitted == canonical
    assert endpoint.payload == payload
    assert progress == [len(values)]


def _binary(spec: str, count: int, payload: bytes) -> bytes:
    from clickhouse_driver.varint import write_varint  # noqa: PLC0415
    from clickhouse_driver.writer import write_binary_str  # noqa: PLC0415

    output = BytesIO()
    write_varint(1, output)
    write_varint(count, output)
    write_binary_str("address", output)
    write_binary_str(spec, output)
    output.write(payload)
    return output.getvalue()


@pytest.fixture
def _matching_ip_decoder(monkeypatch: pytest.MonkeyPatch, transport: str) -> None:
    if transport == "http_python":
        from clickhouse_connect.datatypes import network  # noqa: PLC0415
        from clickhouse_connect.driver import dataconv  # noqa: PLC0415

        # Pair the real Python buffer with the driver's real Python IP decoder,
        # as clickhouse-connect does when its optional C modules are disabled.
        monkeypatch.setattr(network, "data_conv", dataconv)


@pytest.mark.usefixtures("_matching_ip_decoder")
@pytest.mark.parametrize("transport", ["native", "http_python", "http_compiled"])
@pytest.mark.parametrize("exact_source", [False, True])
@pytest.mark.parametrize(("spec", "_values", "canonical", "payload"), _cases())
def test_network_binary_results_preserve_native_family_and_nested_nulls(
    transport: str,
    exact_source: bool,
    spec: str,
    _values: list[Any],
    canonical: list[Any],
    payload: bytes,
) -> None:
    binary = _binary(spec, len(canonical), payload)
    client = (
        _NetworkNativeBinary(binary).client
        if transport == "native"
        else ExactHTTPClient(HTTPBinary(binary, python_buffer=transport == "http_python"))
    )
    if exact_source:
        client.enable_exact_source_values()
    # Unique-key native maps preserve the public dict result shape.
    expected = [
        dict(value.pairs) if isinstance(value, OrderedMapPairs) else value for value in canonical
    ]
    frame = client.query_df("SELECT address")
    assert frame.iloc[:, 0].tolist() == expected
    with client.query_df_stream("SELECT address") as frames:
        assert next(frames).iloc[:, 0].tolist() == expected
        assert list(frames) == []
