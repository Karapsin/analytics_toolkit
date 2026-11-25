"""Native floating underflow errors are detected before live mutation."""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal

import pandas as pd
import pytest
from analytics_toolkit import sql

from tests.sql._support.gp_numeric_text import numeric_text_target


@pytest.mark.parametrize(("native", "small"), [("REAL", "1e-100"), ("DOUBLE PRECISION", "1e-1000")])
@pytest.mark.parametrize("explicit", [False, True])
def test_public_gp_decimal_underflow_rejects_before_any_live_mutation(
    monkeypatch, native, small, explicit
):
    database = numeric_text_target(monkeypatch, native)
    original_rows = deepcopy(database.rows)
    frame = pd.DataFrame(
        {"payload": pd.Series([Decimal("1.25"), None, Decimal(small)], dtype=object)}
    )
    original = frame.copy(deep=True)

    try:
        with pytest.raises(ValueError, match=r"(?i)float|numeric|domain|underflow"):
            sql.load_df(
                "gp",
                "sandbox.events",
                frame,
                write_mode="truncate_insert",
                retry_cnt=1,
                timeout_increment=0,
                **({"table_schema": {"payload": native}} if explicit else {}),
            )
    finally:
        assert database.rows == original_rows
        assert database.live_mutations == []

    pd.testing.assert_frame_equal(frame, original)
    assert all(connection.close_calls == 1 for connection in database.opened)
