"""Apply the supported pandas-1.x nanosecond bound at the real scalar boundary."""

from __future__ import annotations

import pandas as pd
from analytics_toolkit.sql.core import exact_values


def restrict_exact_timestamp_to_nanoseconds(monkeypatch):
    """Keep real pandas construction and use its real checked ns projection.

    Pandas 1.x only constructed nanosecond Timestamp containers. Newer pandas
    accepts wider units, so `.value` supplies the same genuine overflow bound.
    No encoded value, temporal arithmetic or driver result is substituted.
    """
    original = pd.Timestamp

    class TimestampBoundary(type):
        def __instancecheck__(cls, value):
            return isinstance(value, original)

        def __call__(cls, *args, **kwargs):
            result = original(*args, **kwargs)
            _ = result.value
            return result

    class NanosecondTimestamp(metaclass=TimestampBoundary):
        pass

    class PandasBoundary:
        Timestamp = NanosecondTimestamp

        def __getattr__(self, name):
            return getattr(pd, name)

    monkeypatch.setattr(exact_values, "pd", PandasBoundary())
