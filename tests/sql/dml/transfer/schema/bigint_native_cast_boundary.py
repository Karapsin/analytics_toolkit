"""Python BIGINT normalization and native final VARCHAR casts have different grammars."""

from __future__ import annotations

import pytest
from analytics_toolkit.sql.backends.trino.integer_values import TrinoIntegerValueError

from tests.sql._support.adaptive_transfer import track_spools
from tests.sql.dml.transfer.schema.bigint_stage import _SENTINELS, bigint_transfer
from tests.sql.dml.transfer.schema.direct_date_stage import capture_raw_spool_rows

_PYTHON_ONLY = [
    pytest.param("4_2", id="python-underscores"),
    pytest.param("\u00a042\u00a0", id="python-nbsp"),
    pytest.param("\U0001d7dc\U0001d7da", id="python-supplementary-digits"),
]


@pytest.mark.parametrize("text", _PYTHON_ONLY)
def test_direct_bigint_authority_retains_python_integer_conversion(monkeypatch, text):
    values = ["7", None, text]
    raw_rows = [(value,) for value in values]
    captured = capture_raw_spool_rows(monkeypatch)
    scenario, destination = bigint_transfer(
        monkeypatch, values, text_stage=False, text_casts={"7": 7, None: None}
    )
    paths = track_spools(monkeypatch)

    result = scenario.run(metadata=True)

    assert result.rows == len(values)
    assert result.metadata.input_count == len(values)
    assert scenario.loaded == [[(7,), (None,), (42,)]]
    assert destination.rows == [(7,), (None,), (42,)]
    assert all(row[0] is None or type(row[0]) is int for row in destination.rows)
    assert scenario.schemas == [{"id": "BIGINT"}]
    assert scenario.existing_types == {"id": "BIGINT"}
    assert captured == raw_rows
    assert [type(row[0]) for row in captured] == [type(row[0]) for row in raw_rows]
    assert scenario.read_count == 1
    assert scenario.events.index("complete:SELECT id FROM source") < scenario.events.index(
        "create_target"
    )
    assert paths
    assert all(not path.exists() for path in paths)


@pytest.mark.parametrize("text", _PYTHON_ONLY)
def test_varchar_stage_uses_native_bigint_cast_grammar_before_target_clear(monkeypatch, text):
    values = ["7", None, text]
    raw_rows = [(value,) for value in values]
    captured = capture_raw_spool_rows(monkeypatch)
    scenario, destination = bigint_transfer(
        monkeypatch, values, text_stage=True, text_casts={"7": 7, None: None}
    )
    paths = track_spools(monkeypatch)

    with pytest.raises(ValueError, match="resolved destination schema") as failure:
        scenario.run()

    chain = []
    error = failure.value
    while error is not None:
        chain.append(error)
        error = error.__cause__ or (None if error.__suppress_context__ else error.__context__)
    assert any(isinstance(error, TrinoIntegerValueError) for error in chain)
    assert all(text not in str(error) for error in chain)
    assert destination.rows == _SENTINELS
    assert destination.statements == []
    assert scenario.existing_types == {"id": "BIGINT"}
    assert "create_target" not in scenario.events
    assert not scenario.loaded
    assert scenario.finalizations == 0
    assert scenario.read_count == 1
    assert captured == raw_rows
    assert "complete:SELECT id FROM source" in scenario.events
    assert paths
    assert all(not path.exists() for path in paths)
