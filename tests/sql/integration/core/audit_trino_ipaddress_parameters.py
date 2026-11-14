"""Native Trino IPADDRESS casts and 128-bit identities, without IP storage."""

from __future__ import annotations

import os
import uuid
from contextlib import closing
from typing import TYPE_CHECKING

import pytest
from analytics_toolkit.sql.backends.trino.parameters import normalize_value
from analytics_toolkit.sql.connection.get_sql_connection import get_sql_connection

from tests.sql.integration._support.identity import query_label

if TYPE_CHECKING:
    from typing import Any

pytestmark = [pytest.mark.integration, pytest.mark.integration_core]


def _label() -> str:
    return query_label(
        os.environ.get("SQL_INTEGRATION_RUN_ID", uuid.uuid4().hex[:8]),
        os.environ.get("SQL_INTEGRATION_TEST_ID", "manual"),
        "trino-ipaddress-parameter-cast",
    )


def _cases() -> list[tuple[Any, str, str | None, bytes | None]]:
    mapped = bytes.fromhex("00000000000000000000ffffc0000201")
    ipv6 = bytes.fromhex("20010db8000000000000000000000001")
    return [
        ("192.0.2.1", "IPADDRESS '192.0.2.1'", "192.0.2.1", mapped),
        ("2001:0db8:0:0:0:0:0:1", "IPADDRESS '2001:db8::1'", "2001:db8::1", ipv6),
        ("::ffff:192.0.2.1", "IPADDRESS '192.0.2.1'", "192.0.2.1", mapped),
        (b"\xc0\x00\x02\x01", "IPADDRESS '192.0.2.1'", "192.0.2.1", mapped),
        (ipv6, "IPADDRESS '2001:db8::1'", "2001:db8::1", ipv6),
        (None, "CAST(NULL AS IPADDRESS)", None, None),
        (
            "fe80::1%0",
            "IPADDRESS 'fe80::1'",
            "fe80::1",
            bytes.fromhex("fe800000000000000000000000000001"),
        ),
    ]


@pytest.mark.sql_scenario("audit.load.trino_ipaddress_parameters")
def test_trino_ipaddress_parameters_keep_native_canonical_and_binary_identity() -> None:
    cases = _cases()
    casts, results = [], []
    for index, (_value, native, _canonical, _bits) in enumerate(cases):
        casts.extend([f"CAST(? AS IPADDRESS) AS actual_{index}", f"{native} AS native_{index}"])
        results.extend(
            [
                f"actual_{index}",
                f"native_{index}",
                f"actual_{index} IS NOT DISTINCT FROM native_{index}",
                f"CAST(actual_{index} AS VARBINARY)",
            ]
        )
    # Trino 475's IpAddressOperators stores IPv4 as IPv4-mapped 16-byte data.
    # Controls are independently written native literals, never normalized text.
    query = (
        f"/* {_label()} */ WITH casts AS (SELECT "
        + ", ".join(casts)
        + ") SELECT "
        + ", ".join(results)
        + " FROM casts"
    )
    parameters = [
        normalize_value(value, "IPADDRESS") for value, _native, _canonical, _bits in cases
    ]
    with closing(get_sql_connection("trino_source_values")) as connection, closing(
        connection.cursor()
    ) as cursor:
        cursor.execute(query, parameters)
        rows = cursor.fetchall()
    assert len(rows) == 1
    assert len(rows[0]) == len(cases) * 4
    for index, (_value, _native, canonical, bits) in enumerate(cases):
        actual, native, equal, binary = rows[0][index * 4 : index * 4 + 4]
        assert actual == native == canonical
        assert equal is True
        assert binary == bits
        if bits is not None:
            assert isinstance(actual, str)
            assert isinstance(binary, bytes)
            assert len(binary) == 16
