"""Unsupported exact input domains fail before inference can narrow their values."""

from __future__ import annotations

from fractions import Fraction

import numpy as np
import pandas as pd
import pytest
from analytics_toolkit import sql


@pytest.mark.parametrize("backend", ["gp", "trino", "ch"])
@pytest.mark.parametrize(
    "value",
    [
        np.array(["2025-01-01T00:00:00.000000001"], dtype="datetime64[ns]"),
        np.array([1], dtype="timedelta64[ns]"),
        Fraction(9007199254740993, 1),
    ],
    ids=["datetime-array", "duration-array", "exact-real"],
)
def test_create_rejects_unhandled_exact_domains_before_generating_narrow_schema(backend, value):
    frame = pd.DataFrame({"payload": pd.Series([value], dtype=object)})

    with pytest.raises((TypeError, ValueError), match=r"[Uu]nsupported|[Ee]xplicit|[Ee]xact|NumPy"):
        sql.create_table(
            backend,
            "sandbox.exact_input",
            frame,
            only_generate_sql=True,
            ch_distributed_table=False,
        )

    assert frame.iloc[0, 0] is value


@pytest.mark.parametrize("backend", ["gp", "trino", "ch"])
def test_create_rejects_extended_float_before_binary64_rounding(backend):
    if np.finfo(np.longdouble).nmant <= np.finfo(np.float64).nmant:
        pytest.skip("This NumPy platform has no floating precision beyond binary64.")
    value = np.longdouble(2**53) + np.longdouble(1)
    frame = pd.DataFrame({"payload": pd.Series([value], dtype=object)})

    with pytest.raises((TypeError, ValueError), match=r"[Uu]nsupported|[Ee]xplicit|[Ee]xact|NumPy"):
        sql.create_table(
            backend,
            "sandbox.exact_input",
            frame,
            only_generate_sql=True,
            ch_distributed_table=False,
        )

    assert frame.iloc[0, 0] is value
