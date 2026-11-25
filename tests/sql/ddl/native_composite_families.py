"""Existing native codecs must remain eligible during dataframe inference."""

from __future__ import annotations

from ipaddress import IPv4Address, IPv6Address

import pandas as pd
import pytest
from analytics_toolkit import sql


def test_public_create_infers_native_trino_row_map_key():
    payload = {(1, "first"): "one", (2, "second"): "two"}
    frame = pd.DataFrame({"payload": pd.Series([payload, None], dtype=object)})

    ddl = sql.create_table("trino", "sandbox.events", frame, only_generate_sql=True)

    assert 'MAP(ROW("field0" BIGINT, "field1" VARCHAR), VARCHAR)' in ddl
    assert frame.iloc[0, 0] is payload
    assert payload == {(1, "first"): "one", (2, "second"): "two"}


@pytest.mark.parametrize(
    ("values", "native"),
    [
        ([IPv4Address("192.0.2.1"), None], "Nullable(IPv4)"),
        ([IPv6Address("2001:db8::1"), None], "Nullable(IPv6)"),
    ],
)
def test_public_create_infers_native_clickhouse_network_family(values, native):
    frame = pd.DataFrame({"address": pd.Series(values, dtype=object)})

    ddl = sql.create_table(
        "ch", "sandbox.events", frame, only_generate_sql=True, ch_distributed_table=False
    )

    assert native in ddl
    assert frame.iloc[0, 0] is values[0]


@pytest.mark.parametrize(
    ("payload", "native"),
    [
        ([IPv4Address("192.0.2.1"), None], "Array(Nullable(IPv4))"),
        ({"one": IPv6Address("2001:db8::1")}, "Map(String, IPv6)"),
    ],
)
def test_public_create_retains_nested_native_network_candidates(payload, native):
    frame = pd.DataFrame({"payload": pd.Series([payload], dtype=object)})
    ddl = sql.create_table(
        "ch", "sandbox.events", frame, only_generate_sql=True, ch_distributed_table=False
    )
    assert native in ddl
    assert frame.iloc[0, 0] is payload


def test_public_create_keeps_scoped_addresses_in_text_without_erasing_the_scope():
    address = IPv6Address("fe80::1%private-scope")
    frame = pd.DataFrame({"address": pd.Series([address], dtype=object)})
    ddl = sql.create_table(
        "ch", "sandbox.events", frame, only_generate_sql=True, ch_distributed_table=False
    )
    assert "String" in ddl
    assert "IPv6" not in ddl
    assert "private-scope" not in ddl
    assert str(frame.iloc[0, 0]) == "fe80::1%private-scope"
