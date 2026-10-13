"""Report setup and workbook ownership for offline Excel tests."""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

import pandas as pd
from openpyxl import load_workbook

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence
    from pathlib import Path


def report_frame(metric: str, values: Sequence[float], **columns: Any) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "metric": [metric] * len(values),
            "ab_group": ["control", "test_1"],
            "qr_group": ["ALL"] * len(values),
            "start_dt": ["2026-03-30"] * len(values),
            "value": list(values),
            **columns,
        }
    )


@contextmanager
def report_workbook(path: Path, **options: Any) -> Iterator[Any]:
    workbook = load_workbook(path, **options)
    try:
        yield workbook
    finally:
        workbook.close()
