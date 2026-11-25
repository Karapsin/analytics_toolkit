"""Native CH network objects survive the public adaptive transfer checkpoint."""

from __future__ import annotations

import struct
from copy import deepcopy
from ipaddress import IPv4Address, IPv6Address

import pytest
from analytics_toolkit.sql.backends import get_backend_adapter

from tests.conftest import DEFAULT_SQL_CONNECTIONS
from tests.sql._support.adaptive_transfer import RowBatch, SourceColumn, attempt, install
from tests.sql.backends.ch._support.network_transport import encode_normalized


class NativeNetworkTarget:
    is_native_transport = True

    def __init__(self, native_type):
        self.native_type = native_type
        self.emitted = []
        self.emitted_rows = []
        self.payload = b""

    def insert(self, *, table, data, column_names, column_type_names):
        assert table == "sandbox.private_payload"
        assert column_names == ["id", "amount"]
        assert column_type_names == [self.native_type, "Int64"]
        self.emitted_rows = list(data)
        self.emitted = [row[0] for row in data]
        self.payload = encode_normalized(self.native_type, self.emitted, "native")


@pytest.mark.parametrize(
    ("native", "values", "wire"),
    [
        (
            "IPv4",
            [IPv4Address(0), IPv4Address("203.0.113.17"), IPv4Address(2**32 - 1)],
            bytes(3) + struct.pack("<3I", 0, 0xCB007111, 2**32 - 1),
        ),
        (
            "IPv6",
            [IPv6Address(0), IPv6Address("::ffff:203.0.113.17"), IPv6Address(2**128 - 1)],
            bytes(3) + bytes(16) + bytes.fromhex("00000000000000000000ffffcb007111") + b"\xff" * 16,
        ),
    ],
    ids=["native-ipv4", "native-ipv6"],
)
def test_ch_network_replay_public_transfer_keeps_native_addresses_and_exact_wire(
    monkeypatch, write_sql_connections, native, values, wire
):
    connections = deepcopy(DEFAULT_SQL_CONNECTIONS)
    connections["ch_target"] = {**connections["ch"], "database": "destination"}
    write_sql_connections(connections)
    destination = f"Nullable({native})"
    source_sql = "SELECT id, amount FROM source"
    rows = [(value, index) for index, value in enumerate(values)]
    schema = {"id": destination, "amount": "Int64"}
    scenario = install(
        monkeypatch,
        native_type=native,
        rows=rows,
        to_db_key="ch_target",
        to_db_backend="ch",
        source_sql=source_sql,
        table_schema=schema,
    )
    # Network metadata can prove its own destination domain. The selected
    # Int256 column's observed narrow values require the real all-row barrier.
    scenario.source_schema = [SourceColumn("id", native), SourceColumn("amount", "Int256")]
    scenario.batches[source_sql] = [RowBatch(["id", "amount"], [row]) for row in rows]
    target = NativeNetworkTarget(destination)
    load = attempt.load_stage_batches

    def load_through_real_adapter(options, refs, state, *args, **kwargs):
        count = load(options, refs, state, *args, **kwargs)
        get_backend_adapter("ch").insert_rows_batch(
            target,
            "sandbox.private_payload",
            ["id", "amount"],
            scenario.loaded[-1],
            target_column_types=state.stage_column_types,
            trino_insert_chunk_size=None,
            gp_insert_chunk_size=None,
            connection_type="ch",
            query_label=None,
            on_progress=None,
        )
        return count

    monkeypatch.setattr(attempt, "load_stage_batches", load_through_real_adapter)

    result = scenario.run(metadata=True)

    assert result.rows == len(values)
    assert scenario.schemas == [schema]
    assert scenario.loaded == [rows]
    assert target.emitted_rows == rows
    assert all(type(row[1]) is int for row in target.emitted_rows)
    assert target.emitted == values
    assert all(type(actual) is type(expected) for actual, expected in zip(target.emitted, values))
    assert target.payload == wire
    assert scenario.read_count == scenario.source_open_count == 1
    assert scenario.events.index(f"complete:{source_sql}") < scenario.events.index("create_target")
    assert result.metadata.input_count == len(values)
    resolution = result.metadata.type_resolutions[0]
    assert resolution["destination_type"] == destination
    assert resolution["encoding_version"] is None
    assert resolution["certified_input_count"] == len(values)
    assert result.metadata.type_resolutions[1]["destination_type"] == "Int64"
    assert result.metadata.type_resolutions[1]["certified_input_count"] == len(values)
    assert "spool" not in scenario.options.input_resolution
