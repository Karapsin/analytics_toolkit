"""Foreign array inference sees late native GP bounds before publication."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from tests.sql._support.gp_source_codecs import install_source_transfer


@pytest.mark.parametrize("existing", [False, True], ids=["inferred-text", "authoritative-array"])
def test_gp_source_late_nondefault_array_bounds_require_complete_foreign_preflight(
    monkeypatch, existing
):
    scenario, source = install_source_transfer(
        monkeypatch, "INTEGER[]", 1007, ["{1,2}", "[0:1]={3,4}"], frozen=False
    )
    scenario.options = replace(
        scenario.options, to_db_key="trino", to_db_backend="trino", table_schema=None
    )
    if existing:
        scenario.existing_types = {"id": "ARRAY(INTEGER)"}
        with pytest.raises(ValueError, match="array bounds"):
            scenario.run()
        assert scenario.loaded == scenario.schemas == []
        assert "create_target" not in scenario.events
        assert "publish" not in scenario.events
        assert scenario.existing_types == {"id": "ARRAY(INTEGER)"}
    else:
        result = scenario.run(metadata=True)
        assert result.rows == result.metadata.input_count == 2
        assert scenario.schemas == [{"id": "VARCHAR"}]
        first, last = [json.loads(row[0]) for row in scenario.loaded[0]]
        assert first["backend"] == last["backend"] == "gp"
        assert first["value"][0] == last["value"][0] == "native_array"
        assert first["value"][-2:] == [[2], [1]]
        assert last["value"][-2:] == [[2], [0]]
    assert scenario.source_open_count == scenario.read_count == len(source.queries) == 1
    assert len(source.decoded) == 2
    assert source.closed
    assert all(cursor.closed for cursor in source.cursors)
