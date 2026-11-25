"""Public transfers retain actual binary floating values through certification."""

from __future__ import annotations

from copy import deepcopy
from struct import pack

import pytest
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.ch.insert import normalize_rows
from analytics_toolkit.sql.dml.transfer.io.source import iter_source_batches

from tests.conftest import DEFAULT_SQL_CONNECTIONS
from tests.sql._support.adaptive_transfer import Connection, Scenario, attempt, install
from tests.sql._support.load_client_validation import ClientStore
from tests.sql.backends.ch._support.float_values import assert_native_float_values, float_client
from tests.sql.backends.ch._support.native_string_insert import NativeStringEndpoint
from tests.sql.backends.ch._support.network_transport import encode_normalized


@pytest.mark.parametrize("transport", ["native", "http"])
@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("adaptive", [False, True])
def test_public_transfer_preserves_decoded_nan_and_sql_null_before_and_after_freezing(
    monkeypatch, write_sql_connections, transport, nested, adaptive
):
    client, source_state, source_type = float_client(transport, nested=nested)
    connections = deepcopy(DEFAULT_SQL_CONNECTIONS)
    connections["target_ch"] = deepcopy(connections["ch"])
    write_sql_connections(connections)

    def read(  # noqa: PLR0913 -- preserve the source reader's positional retry defaults.
        self,
        key,
        backend,
        reference,
        query,
        batch_size,
        retry_cnt=1,
        timeout_increment=0,
        *args,
        **kwargs,
    ):
        self.read_count += 1
        yield from iter_source_batches(
            key,
            backend,
            reference,
            query,
            batch_size,
            retry_cnt,
            timeout_increment,
            *args,
            **kwargs,
        )

    monkeypatch.setattr(Scenario, "read", read)
    target_type = source_type.replace("Float64", "Float32") if adaptive else source_type
    scenario = install(
        monkeypatch,
        native_type=source_type,
        to_db_key="target_ch",
        to_db_backend="ch",
        write_mode="append",
        table_schema={"id": target_type},
    )
    scenario.existing_types = {"id": target_type}
    monkeypatch.setattr(
        attempt,
        "get_sql_connection",
        lambda key: client if key == "ch" else Connection(),
    )

    count = scenario.run()

    assert count == (1 if nested else 3)
    rows = [row for batch in scenario.loaded for row in batch]
    values = rows[0][0] if nested else [row[0] for row in rows]
    assert_native_float_values(values)
    assert source_state.query_count == 1
    assert scenario.read_count == 1
    bits = "f" if adaptive else "d"
    scalar_payload = b"\x00\x01\x00" + pack(f"<3{bits}", float("nan"), 0.0, -0.0)
    expected = pack("<Q", 3) + scalar_payload if nested else scalar_payload
    if transport == "native":
        target = ClientStore("native", {"id": target_type}, exists=True)
        target.rows.clear()
        endpoint = NativeStringEndpoint(target)
        try:
            get_backend_adapter("ch").insert_rows_batch(
                endpoint.client,
                "sandbox.events",
                ["id"],
                rows,
                target_column_types={"id": target_type},
                trino_insert_chunk_size=None,
                gp_insert_chunk_size=None,
                connection_type="ch",
                query_label=None,
                on_progress=None,
            )
        finally:
            endpoint.client.close()
        assert target.wire_columns == [("id", target_type, expected)]
        assert endpoint.close_calls == 1
    else:
        terminal = normalize_rows(["id"], rows, {"id": target_type}, native_transport=False)
        assert encode_normalized(target_type, [row[0] for row in terminal], transport) == expected
