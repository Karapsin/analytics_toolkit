"""Native GP NUMERIC NaN cannot follow a finite-only foreign streaming proof."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest
from analytics_toolkit.sql.backends.native_scalars import NativeDecimalNaN

from tests.sql._support.gp_source_codecs import install_source_transfer


def _source(monkeypatch, backend, existing):
    scenario, source = install_source_transfer(
        monkeypatch,
        "NUMERIC(18,2)",
        1700,
        ["1.25", "NaN"],
        frozen=False,
        precision=18,
        scale=2,
    )
    scenario.options = replace(
        scenario.options, to_db_key=backend, to_db_backend=backend, table_schema=None
    )
    if existing:
        scenario.existing_types = {"id": "Decimal(18,2)" if backend == "ch" else "DECIMAL(18,2)"}
    return scenario, source


@pytest.mark.parametrize("backend", ["ch", "trino"])
def test_gp_numeric_nan_inference_profiles_complete_source_before_text_publication(
    monkeypatch, backend
):
    scenario, source = _source(monkeypatch, backend, False)

    result = scenario.run(metadata=True)

    assert result.rows == result.metadata.input_count == 2
    assert scenario.source_open_count == scenario.read_count == len(source.queries) == 1
    assert source.decoded[0] == (Decimal("1.25"),)
    assert isinstance(source.decoded[1][0], NativeDecimalNaN)
    assert scenario.loaded == [[("1.25",), ("NaN",)]]
    native = scenario.schemas[0]["id"].lower()
    assert "string" in native or "varchar" in native
    assert "float" not in native
    assert source.closed
    assert all(cursor.closed for cursor in source.cursors)


@pytest.mark.parametrize("backend", ["ch", "trino"])
def test_gp_numeric_nan_rejects_foreign_existing_decimal_before_any_mutation(monkeypatch, backend):
    scenario, source = _source(monkeypatch, backend, True)
    original = dict(scenario.existing_types)

    with pytest.raises(ValueError, match=r"decimal|numeric|schema|nonfinite"):
        scenario.run()

    assert scenario.source_open_count == scenario.read_count == len(source.queries) == 1
    assert len(source.decoded) == 2
    assert isinstance(source.decoded[1][0], NativeDecimalNaN)
    assert scenario.existing_types == original
    assert scenario.schemas == scenario.loaded == []
    assert "create_target" not in scenario.events
    assert "publish" not in scenario.events
    assert source.closed
    assert all(cursor.closed for cursor in source.cursors)
