"""Finite native source floats must survive low server text output precision."""

from __future__ import annotations

import struct

import pytest
from analytics_toolkit.sql.backends.native_scalars import ExactArray

from tests.sql._support.gp_float_wire import float_source


@pytest.mark.parametrize("oid", [700, 701, 1022], ids=["float4", "float8", "bounded-float8-array"])
def test_gp_float_source_preserves_ieee_payload_without_changing_query_settings(monkeypatch, oid):
    scenario, source, exact = float_source(monkeypatch, oid)

    result = scenario.run(metadata=True)

    assert result.rows == 1
    assert scenario.read_count == scenario.source_open_count == len(source.executed_inputs) == 1
    value = scenario.loaded[0][0][0]
    if oid == 1022:
        assert isinstance(value, ExactArray)
        assert value.dimensions == (2,)
        assert value.lower_bounds == (-1,)
        assert value.values[1] is None
        value = value.values[0]
    code = "!f" if oid == 700 else "!d"
    assert struct.pack(code, value) == struct.pack(code, exact)
    assert source.closed
    assert all(cursor.closed for cursor in source.cursors)
