"""Deterministic input frames; expected statistics stay in the tests."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd

if TYPE_CHECKING:
    from collections.abc import Sequence


def metric_frame(groups: Sequence[str], **metrics: Any) -> pd.DataFrame:
    return pd.DataFrame(
        {"user_id": list(range(1, len(groups) + 1)), "group_name": list(groups), **metrics}
    )


def sample_metrics_df() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "user_id": list(range(1, 13)),
            "group_name": [
                "control",
                "control",
                "control",
                "control",
                "test_a",
                "test_a",
                "test_a",
                "test_a",
                "test_b",
                "test_b",
                "test_b",
                "test_b",
            ],
            "orders": [10, 12, 9, np.nan, 13, 15, 11, 14, 8, 10, 9, 11],
            "gmv": [
                100.0,
                120.0,
                95.0,
                110.0,
                130.0,
                145.0,
                118.0,
                140.0,
                92.0,
                105.0,
                99.0,
                108.0,
            ],
            "clicks": [5, 3, 4, 2, 7, 5, 6, 8, 4, 5, 3, 4],
            "impressions": [10, 8, 0, 4, 14, 10, 12, 16, 8, 10, 6, 8],
        }
    )
