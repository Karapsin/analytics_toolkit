"""Disposable native CH address values through both configured transports."""

from __future__ import annotations

import ipaddress
import os
import uuid
from typing import TYPE_CHECKING

import pandas as pd
import pytest
from analytics_toolkit import sql

from tests.sql.integration._support.backends import integration_table
from tests.sql.integration._support.identity import query_label

if TYPE_CHECKING:
    from typing import Any

    from tests.sql.integration._support.resources import ResourceRegistry

pytestmark = [pytest.mark.integration, pytest.mark.integration_core]

_SCHEMA = {"row_id": "Int64", "v4": "Nullable(IPv4)", "v6": "Nullable(IPv6)"}
_V6_ONE = "20010DB8000000000000000000000001"


def _label(purpose: str) -> str:
    return query_label(
        os.environ.get("SQL_INTEGRATION_RUN_ID", uuid.uuid4().hex[:8]),
        os.environ.get("SQL_INTEGRATION_TEST_ID", "manual"),
        purpose,
    )


def _owned_table(alias: str, registry: ResourceRegistry) -> str:
    table = integration_table("ch", "ip_values")
    acknowledged = False

    def cleanup() -> None:
        if acknowledged:
            sql.execute(
                alias,
                f"DROP TABLE IF EXISTS {table}",
                retry_cnt=1,
                query_label=_label("ch-ip-values-cleanup"),
            )

    # Register before submission; only a plain CREATE acknowledgement owns the
    # table. A confirmed collision or ambiguous submission never authorizes DROP.
    registry.finalizer(cleanup)
    sql.execute(
        alias,
        f"CREATE TABLE {table} (row_id Int64, v4 Nullable(IPv4), v6 Nullable(IPv6)) "
        "ENGINE = MergeTree ORDER BY row_id",
        retry_cnt=1,
        query_label=_label("ch-ip-values-create"),
    )
    acknowledged = True
    return table


def _frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "row_id": range(1, 10),
            "v4": pd.Series(
                [
                    None,
                    "192.0.2.1",
                    ipaddress.IPv4Address("198.51.100.2"),
                    0,
                    2**32 - 1,
                    "203.0.113.3",
                    "192.0.2.4",
                    None,
                    ipaddress.IPv4Address("0.0.0.6"),
                ],
                dtype=object,
            ),
            "v6": pd.Series(
                [
                    None,
                    "2001:db8::1",
                    ipaddress.IPv6Address("2001:db8::2"),
                    0,
                    2**128 - 1,
                    bytes.fromhex("20010db8000000000000000000000003"),
                    1,
                    ipaddress.IPv4Address("192.0.2.5"),
                    None,
                ],
                dtype=object,
            ),
        }
    )


def _expected_rows() -> list[tuple[int, str | None, str | None]]:
    return [
        (1, None, None),
        (2, "192.0.2.1", _V6_ONE),
        (3, "198.51.100.2", "20010DB8000000000000000000000002"),
        (4, "0.0.0.0", "0" * 32),
        (5, "255.255.255.255", "F" * 32),
        (6, "203.0.113.3", "20010DB8000000000000000000000003"),
        (7, "192.0.2.4", "00000000000000000000000000000001"),
        (8, None, "00000000000000000000FFFFC0000205"),
        (9, "0.0.0.6", None),
    ]


def _rows(alias: str, table: str) -> list[tuple[int, str | None, str | None]]:
    actual = sql.read(
        alias,
        "SELECT src.row_id, toString(src.v4) AS v4_text, "
        "hex(IPv6StringToNum(toString(src.v6))) AS v6_bits, "
        "toTypeName(src.v4) AS type_v4, toTypeName(src.v6) AS type_v6 "
        f"FROM {table} AS src ORDER BY src.row_id",
        retry_cnt=1,
        query_label=_label("ch-ip-values-check"),
    )
    rows = []
    for row_id, v4, v6_bits, type_v4, type_v6 in actual.itertuples(index=False, name=None):
        assert type_v4 == "Nullable(IPv4)"
        assert type_v6 == "Nullable(IPv6)"
        rows.append(
            (
                int(row_id),
                None if pd.isna(v4) else v4,
                None if pd.isna(v6_bits) else v6_bits,
            )
        )
    return rows


def _load(alias: str, table: str, frame: pd.DataFrame, *, explicit: bool) -> int:
    options: dict[str, Any] = {"table_schema": _SCHEMA} if explicit else {}
    return sql.load_df(
        alias,
        table,
        frame,
        write_mode="truncate_insert",
        retry_cnt=1,
        query_label=_label("ch-ip-values-load"),
        ch_distributed_table=False,
        order_by="row_id",
        **options,
    )


@pytest.mark.sql_scenario("audit.load.clickhouse_ip_values")
def test_clickhouse_ip_columns_keep_native_values_before_authoritative_mutation(
    resource_registry: ResourceRegistry,
) -> None:
    # ch_target follows SQL_INTEGRATION_CLICKHOUSE_DRIVER, so the existing
    # integration runner executes this same scenario with HTTP and native.
    alias = "ch_target"
    table = _owned_table(alias, resource_registry)
    sql.execute(
        alias,
        f"INSERT INTO {table} SELECT toInt64(99), toIPv4('198.51.100.99'), toIPv6('2001:db8::99')",
        retry_cnt=1,
        query_label=_label("ch-ip-values-seed"),
    )
    assert _rows(alias, table) == [(99, "198.51.100.99", "20010DB8000000000000000000000099")]

    for explicit in (True, False):
        incoming = _frame()
        original = incoming.copy(deep=True)
        assert _load(alias, table, incoming, explicit=explicit) == len(incoming)
        pd.testing.assert_frame_equal(incoming, original)
        assert _rows(alias, table) == _expected_rows()

        for column, bad_value in (
            ("v4", "192.0.2.999"),
            ("v6", "2001:db8::g"),
            ("v6", "fe80::1%3"),
        ):
            invalid = pd.DataFrame(
                {"row_id": [10, 11], "v4": ["192.0.2.1"] * 2, "v6": ["2001:db8::1"] * 2}
            )
            invalid.loc[1, column] = bad_value
            with pytest.raises(ValueError, match=r"IPv[46]|IP address"):
                _load(alias, table, invalid, explicit=explicit)
            assert _rows(alias, table) == _expected_rows()
