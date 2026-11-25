"""Visible catalog-name domains retain constraints before destructive writes."""

from __future__ import annotations

from copy import deepcopy

import pandas as pd
import pytest
from analytics_toolkit import sql

from tests.sql._support.gp_catalog_identity import shadow_domain_target


def test_gp_visible_int4_domain_is_certified_before_target_clear(monkeypatch):
    database = shadow_domain_target(monkeypatch)
    original = deepcopy(database.target.rows)
    frame = pd.DataFrame({"payload": pd.Series([1], dtype=object)})

    with pytest.raises(ValueError, match="native assignment"):
        sql.load_df(
            "gp",
            "sandbox.events",
            frame,
            write_mode="truncate_insert",
            retry_cnt=1,
            timeout_increment=0,
        )

    assert database.target.rows == original
    assert database.target.schema == {"payload": "int4"}
    assert database.live_mutations == []
    assert frame.iloc[0, 0] == 1
