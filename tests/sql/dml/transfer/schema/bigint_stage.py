"""Direct and textual Trino BIGINT stages certify exact values before mutation."""

from __future__ import annotations

import pandas as pd
import pytest
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.models import StageFinalizationRequest
from analytics_toolkit.sql.dml.transfer.flow import attempt, finalize
from sqlglot import exp, parse_one

from tests.sql._support.adaptive_transfer import install, track_spools
from tests.sql.dml.transfer.schema.direct_date_stage import capture_raw_spool_rows

_MINIMUM = -(2**63)
_MAXIMUM = 2**63 - 1
_SENTINELS = [(97,), (-97,), (None,)]


class BigintDestination:
    def __init__(self, scenario, text_casts):
        self.scenario = scenario
        self.text_casts = text_casts
        self.rows = list(_SENTINELS)
        self.statements = []

    def cursor(self):
        return BigintCursor(self)

    def commit(self):
        pass

    def rollback(self):
        pass


class BigintCursor:
    description = None
    rowcount = -1

    def __init__(self, destination):
        self.destination = destination

    def execute(self, statement):
        target = self.destination
        target.statements.append(statement)
        parsed = parse_one(statement, read="trino")
        if isinstance(parsed, (exp.Delete, exp.TruncateTable)):
            target.rows.clear()
        elif isinstance(parsed, exp.Insert):
            cast_types = [cast.args["to"] for cast in parsed.find_all(exp.Cast)]
            assert any(native.is_type(exp.DataType.Type.BIGINT) for native in cast_types)
            converted = []
            for row in target.scenario.loaded[-1]:
                value = row[0]
                if isinstance(value, str):
                    if value not in target.text_casts:
                        message = "Destination BIGINT cast rejected staged text."
                        raise ValueError(message)
                    value = target.text_casts[value]
                if value is not None and (
                    type(value) is not int or not _MINIMUM <= value <= _MAXIMUM
                ):
                    message = "Destination BIGINT requires a signed 64-bit integer."
                    raise ValueError(message)
                converted.append((value,))
            target.rows[:] = converted
            self.rowcount = len(converted)

    def close(self):
        pass


def bigint_transfer(monkeypatch, values, *, text_stage, text_casts):
    stage_type = "VARCHAR" if text_stage else "BIGINT"
    scenario = install(
        monkeypatch,
        native_type="VARCHAR",
        rows=[(value,) for value in values],
        from_db_key="trino",
        from_db_backend="trino",
        to_db_key="trino",
        to_db_backend="trino",
        table_schema={"id": stage_type},
        write_mode="truncate_insert",
        replace_target_table=True,
    )
    scenario.existing_types = {"id": "BIGINT"}
    destination = BigintDestination(scenario, text_casts)

    def publish(options, connection_refs, stage_state, total_rows, **_kwargs):
        del connection_refs, stage_state, total_rows
        scenario.finalizations += 1
        get_backend_adapter("trino").finalize_stage_table(
            StageFinalizationRequest(
                connection=destination,
                stage_table="sandbox.incoming_stage",
                target_table=options.target_table,
                replace_target_table=options.replace_target_table,
                target_exists=True,
                sample_batch=pd.DataFrame(columns=["id"]),
                insert_column_types={"id": "BIGINT"},
                write_mode=options.write_mode,
            )
        )

    monkeypatch.setattr(attempt, "finalize_loaded_stage", publish)
    monkeypatch.setattr(finalize, "finalize_loaded_stage", publish)
    return scenario, destination


@pytest.mark.parametrize("text_stage", [False, True], ids=["direct-bigint", "varchar-stage"])
def test_bigint_transfer_certifies_canonical_text_and_bounds_without_changing_raw_input(
    monkeypatch, text_stage
):
    values = ["42", "-7", "0", str(_MINIMUM), str(_MAXIMUM), None]
    expected = [42, -7, 0, _MINIMUM, _MAXIMUM, None]
    raw_rows = [(value,) for value in values]
    captured = capture_raw_spool_rows(monkeypatch)
    text_casts = dict(zip(values, expected))
    scenario, destination = bigint_transfer(
        monkeypatch, values, text_stage=text_stage, text_casts=text_casts
    )
    paths = track_spools(monkeypatch)

    result = scenario.run(metadata=True)

    assert result.rows == len(values)
    assert result.metadata.input_count == len(values)
    stage_type = "VARCHAR" if text_stage else "BIGINT"
    stage_values = values if text_stage else expected
    assert scenario.schemas == [{"id": stage_type}]
    assert scenario.existing_types == {"id": "BIGINT"}
    assert scenario.loaded == [[(value,) for value in stage_values]]
    stage_class = str if text_stage else int
    assert all(row[0] is None or type(row[0]) is stage_class for row in scenario.loaded[0])
    assert destination.rows == [(value,) for value in expected]
    assert all(row[0] is None or type(row[0]) is int for row in destination.rows)
    assert captured == raw_rows
    assert [type(row[0]) for row in captured] == [type(row[0]) for row in raw_rows]
    assert [(value,) for value in values] == raw_rows
    assert scenario.read_count == 1
    assert scenario.finalizations == 1
    assert scenario.events.index("complete:SELECT id FROM source") < scenario.events.index(
        "create_target"
    )
    assert paths
    assert all(not path.exists() for path in paths)


@pytest.mark.parametrize("text_stage", [False, True], ids=["direct-bigint", "varchar-stage"])
@pytest.mark.parametrize(
    "invalid_text",
    [
        pytest.param("private-bigint-6147", id="malformed-private"),
        pytest.param(str(_MINIMUM - 1), id="underflow"),
        pytest.param(str(_MAXIMUM + 1), id="overflow"),
    ],
)
def test_bigint_transfer_rejects_late_invalid_or_out_of_range_input_before_mutation(
    monkeypatch, text_stage, invalid_text
):
    values = ["42", None, invalid_text]
    raw_rows = [(value,) for value in values]
    captured = capture_raw_spool_rows(monkeypatch)
    scenario, destination = bigint_transfer(
        monkeypatch, values, text_stage=text_stage, text_casts={"42": 42, None: None}
    )
    paths = track_spools(monkeypatch)

    with pytest.raises(ValueError, match=r"(?i)BIGINT|integer|destination schema") as failure:
        scenario.run()

    if invalid_text.startswith("private"):
        error = failure.value
        while error is not None:
            assert invalid_text not in str(error)
            error = error.__cause__ or (None if error.__suppress_context__ else error.__context__)
    assert destination.rows == _SENTINELS
    assert destination.statements == []
    assert scenario.existing_types == {"id": "BIGINT"}
    assert "create_target" not in scenario.events
    assert not scenario.loaded
    assert scenario.finalizations == 0
    assert scenario.read_count == 1
    assert len(captured) >= 2
    assert captured == raw_rows[: len(captured)]
    expected_types = [type(row[0]) for row in raw_rows[: len(captured)]]
    assert [type(row[0]) for row in captured] == expected_types
    assert paths
    assert all(not path.exists() for path in paths)
