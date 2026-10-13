"""Unattended UI discovery stays offline; individual cases override its data."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pandas as pd
import pytest
from analytics_toolkit import sql

if TYPE_CHECKING:
    from collections.abc import Callable


@pytest.fixture(autouse=True)
def offline_metadata_reads(monkeypatch: pytest.MonkeyPatch) -> Callable[..., pd.DataFrame]:
    def read(_db_key: str, _query: str, **kwargs: object) -> pd.DataFrame:
        label = kwargs.get("query_label", "")
        assert isinstance(label, str), "SQL query labels must be strings"
        assert label.startswith("sql_explorer metadata="), (
            "Explorer SQL reads outside metadata discovery require an explicit fake"
        )
        return pd.DataFrame()

    monkeypatch.setattr(sql, "read", read)
    return read
