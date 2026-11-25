"""Authoritative GP numeric conversion keeps established float spelling intent."""

from __future__ import annotations

from decimal import Decimal

import pytest

from tests.sql._support.adaptive_transfer import install
from tests.sql.dml.transfer.schema.direct_date_stage import capture_raw_spool_rows


@pytest.mark.parametrize("explicit", [False, True], ids=["existing", "explicit"])
def test_gp_authoritative_numeric_float_assignment_preserves_raw_frozen_input(
    monkeypatch, explicit
):
    captured = capture_raw_spool_rows(monkeypatch)
    rows = [(1.5,), (None,)]
    scenario = install(
        monkeypatch,
        native_type="Float64",
        rows=rows,
        to_db_key="gp",
        to_db_backend="gp",
        table_schema={"id": "NUMERIC(10,1)"} if explicit else None,
    )
    scenario.existing_types = {"id": "NUMERIC(10,1)"}

    result = scenario.run(metadata=True)

    assert result.rows == result.metadata.input_count == 2
    assert scenario.loaded == [[(Decimal("1.5"),), (None,)]]
    assert captured == rows
    assert type(captured[0][0]) is float
    assert scenario.read_count == scenario.source_open_count == 1
    assert rows == [(1.5,), (None,)]
