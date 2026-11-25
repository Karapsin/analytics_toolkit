"""Certified native NaN keys remain distinct from SQL NULL inputs."""

from __future__ import annotations

from decimal import Decimal

import pandas as pd
import pytest
from analytics_toolkit.sql.backends.native_scalars import NativeDecimalNaN, NativeFloatNaN
from analytics_toolkit.sql.dml.load.key_validation import validate_key_nulls


@pytest.mark.parametrize("value", [NativeFloatNaN(float("nan")), NativeDecimalNaN(Decimal("NaN"))])
def test_load_key_null_validation_preserves_certified_native_nan(value):
    frame = pd.DataFrame({"key": pd.Series([value], dtype=object)})

    validate_key_nulls(frame, ["key"])

    assert frame.iloc[0, 0] is value


@pytest.mark.parametrize("value", [float("nan"), Decimal("NaN"), None, pd.NA])
def test_load_key_null_validation_keeps_the_incoming_missing_value_policy(value):
    frame = pd.DataFrame({"key": pd.Series([value], dtype=object)})

    with pytest.raises(ValueError, match="Null key values"):
        validate_key_nulls(frame, ["key"])
