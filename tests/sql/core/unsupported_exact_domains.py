"""Real framed replay must never certify a narrowed unsupported scalar domain."""

from __future__ import annotations

import math
from fractions import Fraction

import numpy as np
import pytest
from analytics_toolkit.sql.backends.value_profiles import ColumnProfile
from analytics_toolkit.sql.core.exact_values import is_null
from analytics_toolkit.sql.core.replay_spool import ReplaySpool


@pytest.mark.parametrize(
    "value",
    [
        np.array(["2025-01-01T00:00:00.000000001"], dtype="datetime64[ns]"),
        np.array([1], dtype="timedelta64[ns]"),
        Fraction(9007199254740993, 1),
    ],
    ids=["datetime-array", "duration-array", "exact-real"],
)
def test_replay_rejects_unhandled_exact_domains_without_certifying_altered_cells(value):
    with ReplaySpool(["payload"]) as spool:
        with pytest.raises(
            (TypeError, ValueError), match=r"[Uu]nsupported|[Ee]xplicit|[Ee]xact|NumPy"
        ):
            spool.write_rows([(value,)])

        assert not spool.certified
        assert spool.closed
        assert not spool.path.exists()


def test_replay_rejects_extended_float_before_certification():
    if np.finfo(np.longdouble).nmant <= np.finfo(np.float64).nmant:
        pytest.skip("This NumPy platform has no floating precision beyond binary64.")
    value = np.longdouble(2**53) + np.longdouble(1)

    with ReplaySpool(["payload"]) as spool:
        with pytest.raises(
            (TypeError, ValueError), match=r"[Uu]nsupported|[Ee]xplicit|[Ee]xact|NumPy"
        ):
            spool.write_rows([(value,)])

        assert not spool.certified
        assert spool.closed
        assert not spool.path.exists()


@pytest.mark.parametrize("value", [float("nan"), np.float64("nan"), np.longdouble("nan")])
def test_ordinary_dataframe_nan_remains_missing_in_every_supported_real_container(value):
    with ReplaySpool(["payload"]) as spool:
        spool.write_rows([(value,), (None,)])
        spool.certify()
        rows = list(spool.iter_rows())
    assert math.isnan(rows[0][0])
    assert is_null(rows[0][0])
    assert rows[1] == (None,)
    profile = ColumnProfile()
    for row in rows:
        profile.add(row[0])
    assert profile.null_count == 2
    assert profile.families == set()
