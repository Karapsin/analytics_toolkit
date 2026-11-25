"""Public GP inference retains native family identity from DBAPI descriptions."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from tests.sql._support.gp_source_codecs import install_source_transfer


@pytest.mark.parametrize(
    "case",
    [
        ("INET", 869, ["192.0.2.1/24", None], ["192.0.2.1/24", None], None, None),
        ("TIME", 1083, [None, None], [None, None], None, None),
        ("NUMERIC", 1700, ["1.25", None], [Decimal("1.25"), None], 65535, 65535),
    ],
    ids=["inet-address", "null-only-time", "unconstrained-numeric-sentinel"],
)
def test_gp_inferred_transfer_retains_native_family_from_actual_description(monkeypatch, case):
    native, oid, documents, expected, precision, scale = case
    scenario, source = install_source_transfer(
        monkeypatch, native, oid, documents, frozen=False, precision=precision, scale=scale
    )
    scenario.options = replace(scenario.options, table_schema=None)

    result = scenario.run(metadata=True)

    assert result.rows == 2
    assert len(scenario.schemas) == 1
    assert scenario.schemas[0]["id"].upper() == native
    assert scenario.loaded == [[(value,) for value in expected]]
    assert scenario.source_open_count == scenario.read_count == len(source.queries) == 1
    assert source.closed
    assert all(cursor.closed for cursor in source.cursors)
