"""Native source identities remain immutable before profiling and replay."""

from __future__ import annotations

import struct
from dataclasses import FrozenInstanceError

import pytest
from analytics_toolkit.sql.backends.gp.json_values import JsonDocument as GpJsonDocument
from analytics_toolkit.sql.core.native_scalars import (
    ExactArray,
    ExactInterval,
    JsonDocument,
    NativeFloatNaN,
)


def test_json_document_keeps_existing_import_identity_and_raw_lexemes():
    assert GpJsonDocument is JsonDocument
    document = JsonDocument(' {"x":1.0000000000000000001,"x":null} ')
    assert str(document) == ' {"x":1.0000000000000000001,"x":null} '
    with pytest.raises(AttributeError):
        document.mutable = True


def test_native_nan_retains_actual_ieee_payload_bits():
    bits = bytes.fromhex("010000000000f8ff")
    decoded = struct.unpack("<d", bits)[0]
    assert struct.pack("<d", NativeFloatNaN(decoded)) == bits
    with pytest.raises(ValueError, match="decoded NaN"):
        NativeFloatNaN(1.0)


def test_native_interval_keeps_months_days_and_clock_independent():
    value = ExactInterval(1, -30, 1234567)
    assert (value.months, value.days, value.microseconds) == (1, -30, 1234567)
    with pytest.raises(FrozenInstanceError):
        value.months = 0
    with pytest.raises(ValueError, match="integers"):
        ExactInterval(True, 0, 0)


def test_exact_array_keeps_nondefault_bounds_and_flat_order():
    value = ExactArray((10, None, 20, 30), (2, 2), (0, -1))
    assert value.values == (10, None, 20, 30)
    assert value.dimensions == (2, 2)
    assert value.lower_bounds == (0, -1)
    assert ExactArray((), (), ()).values == ()
    with pytest.raises(FrozenInstanceError):
        value.lower_bounds = (1, 1)


@pytest.mark.parametrize(
    ("values", "dimensions", "bounds"),
    [([1], (1,), (1,)), ((1,), (1,), ()), ((1,), (-1,), (1,)), ((1,), (2,), (1,))],
)
def test_exact_array_rejects_inconsistent_or_mutable_shape(values, dimensions, bounds):
    with pytest.raises(ValueError, match="Exact array"):
        ExactArray(values, dimensions, bounds)
