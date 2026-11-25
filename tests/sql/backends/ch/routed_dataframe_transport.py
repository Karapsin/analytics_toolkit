from __future__ import annotations

from decimal import Decimal

import pandas as pd
import pytest
from analytics_toolkit.sql.core.exact_values import ExactTemporal

from tests.sql.backends.ch._support.routed_dataframe_transport import (
    decoded_columns,
    publish_dataframe,
)


@pytest.mark.parametrize("scale", range(10))
@pytest.mark.parametrize("routed", [False, True], ids=["direct", "routed"])
def test_routed_dataframe_transport_preserves_all_timestamp_scales(
    scale: int, routed: bool
) -> None:
    schema = {"event_at": f"Nullable(DateTime64({scale}, 'UTC'))"}
    ticks = [-1, None, 1, 1_700_000_000 * 10**scale + (10**scale - 1)]
    values = [None if tick is None else ExactTemporal(tick, scale, "UTC") for tick in ticks]
    frame = pd.DataFrame({"event_at": pd.Series(values, dtype=object)})

    endpoint = publish_dataframe(schema, frame, routed=routed)

    actual = decoded_columns(endpoint)[0]
    assert actual[1] is None
    assert [None if value is None else value.value for value in actual] == [
        None if tick is None else tick * 10 ** (9 - scale) for tick in ticks
    ]


@pytest.mark.parametrize("routed", [False, True], ids=["direct", "routed"])
def test_routed_dataframe_transport_keeps_nullable_exact_scalar_columns(routed: bool) -> None:
    schema = {
        "identifier": "Nullable(UInt64)",
        "amount": "Nullable(Decimal(38, 9))",
        "signed": "Nullable(Int64)",
        "payload": "Nullable(String)",
    }
    columns = {
        "identifier": [2**64 - 1, None, 2**53 + 1],
        "amount": [Decimal("12345678901234567890.123456789"), None, Decimal("0.0001")],
        "signed": [-(2**63), None, -1],
        "payload": [b"\x00\xff", None, b""],
    }
    frame = pd.DataFrame(
        {name: pd.Series(values, dtype=object) for name, values in columns.items()}
    )

    endpoint = publish_dataframe(schema, frame, routed=routed)

    assert decoded_columns(endpoint, bytes_column="payload") == list(columns.values())


@pytest.mark.parametrize("routed", [False, True], ids=["direct", "routed"])
def test_routed_dataframe_transport_uses_real_sdk_metadata_operation_boundary(routed: bool) -> None:
    schema = {"identifier": "Nullable(UInt64)"}
    frame = pd.DataFrame({"identifier": pd.Series([2**64 - 1, None], dtype=object)})

    endpoint = publish_dataframe(schema, frame, routed=routed, resolve_types=True)

    assert decoded_columns(endpoint) == [[2**64 - 1, None]]
    assert [sql for sql in endpoint.queries if sql.startswith("DESCRIBE TABLE ")] == [
        "DESCRIBE TABLE db.events"
    ]
