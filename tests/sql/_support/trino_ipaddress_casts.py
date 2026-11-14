"""Native IPADDRESS target state over actual SDK parameter expressions."""

from __future__ import annotations

import ipaddress

from analytics_toolkit.sql.backends import get_backend_adapter
from sqlglot import exp, parse_one

from tests.sql._support.load_table import load_df_module
from tests.sql._support.trino_time_values import native_time_target


def native_ipaddress_target(monkeypatch):
    database = native_time_target(monkeypatch, "IPADDRESS")
    database.table_rows[database.target] = ["198.51.100.2"]

    def cast(wire):
        assert "CAST(? AS IPADDRESS)" in database.statements[-1][0]
        node = parse_one(wire, read="trino")
        if isinstance(node, exp.Null):
            return None
        if isinstance(node, exp.HexString):
            value = bytes.fromhex(node.this)
        else:
            assert isinstance(node, exp.Literal)
            assert node.is_string
            value = node.this
        address = ipaddress.ip_address(value)
        # Trino's IPADDRESS-to-VARCHAR cast renders IPv4-mapped storage as IPv4.
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        return str(address)

    monkeypatch.setattr(database, "decode_cast", cast)
    return database


def native_ipaddress_key_target(monkeypatch, *, partitioned):
    """Stop at the first stage creation after real existing-schema certification."""
    database = native_ipaddress_target(monkeypatch)
    database.table_rows[database.target] = []
    database.stage_attempts = []
    column_types = {"payload": "IPADDRESS"}
    if partitioned:
        column_types["month"] = "VARCHAR"
    monkeypatch.setattr(
        load_df_module, "get_table_column_types", lambda *_a, **_k: dict(column_types)
    )
    monkeypatch.setattr(
        get_backend_adapter("trino"),
        "get_writable_table_column_types",
        lambda *_a, **_k: dict(column_types),
    )

    def reject_stage(**options):
        database.stage_attempts.append(options["batch"].copy())
        message = "Native IPADDRESS aliases reached SQL stage creation before key rejection."
        raise AssertionError(message)

    monkeypatch.setattr(load_df_module, "create_stage_table", reject_stage)
    return database
