"""Authoritative Trino BOOLEAN conversion preserves the selected raw input."""

from __future__ import annotations

import pandas as pd
import pytest
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.models import StageFinalizationRequest
from analytics_toolkit.sql.backends.trino.boolean_values import TrinoBooleanValueError
from analytics_toolkit.sql.dml.transfer.flow import attempt, finalize
from sqlglot import exp, parse_one

from tests.sql._support.adaptive_transfer import install, track_spools
from tests.sql.dml.transfer.schema.direct_date_stage import capture_raw_spool_rows

_SENTINELS = [(False,), (True,), (None,)]


class BooleanDestination:
    def __init__(self, scenario):
        self.scenario = scenario
        self.rows = list(_SENTINELS)
        self.statements = []

    def cursor(self):
        return BooleanCursor(self)

    def commit(self):
        pass

    def rollback(self):
        pass


class BooleanCursor:
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
            rows = target.scenario.loaded[-1]
            if any(row[0] is not None and type(row[0]) is not bool for row in rows):
                message = "Destination BOOLEAN requires normalized Boolean stage values."
                raise ValueError(message)
            target.rows[:] = rows
            self.rowcount = len(rows)

    def close(self):
        pass


def boolean_target_transfer(monkeypatch, native_type, values, *, explicit_stage):
    scenario = install(
        monkeypatch,
        native_type=native_type,
        rows=[(value,) for value in values],
        from_db_key="trino",
        from_db_backend="trino",
        to_db_key="trino",
        to_db_backend="trino",
        table_schema={"id": "BOOLEAN"} if explicit_stage else None,
        write_mode="truncate_insert",
        replace_target_table=True,
    )
    scenario.existing_types = {"id": "BOOLEAN"}
    destination = BooleanDestination(scenario)

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
                insert_column_types={"id": "BOOLEAN"},
                write_mode=options.write_mode,
            )
        )

    monkeypatch.setattr(attempt, "finalize_loaded_stage", publish)
    monkeypatch.setattr(finalize, "finalize_loaded_stage", publish)
    return scenario, destination


@pytest.mark.parametrize("explicit_stage", [False, True])
@pytest.mark.parametrize(
    ("native_type", "values", "expected"),
    [
        pytest.param(
            "VARCHAR",
            ["true", "false", "t", "f", "1", "0", "TRUE", "False", "T", "F", None],
            [True, False, True, False, True, False, True, False, True, False, None],
            id="canonical-ascii-text",
        ),
        pytest.param(
            "BIGINT", [0, 1, -7, 42, None], [False, True, True, True, None], id="native-integers"
        ),
    ],
)
def test_authoritative_boolean_transfer_converts_one_frame_without_changing_raw_input(
    monkeypatch, explicit_stage, native_type, values, expected
):
    raw_rows = [(value,) for value in values]
    captured = capture_raw_spool_rows(monkeypatch)
    scenario, destination = boolean_target_transfer(
        monkeypatch, native_type, values, explicit_stage=explicit_stage
    )
    paths = track_spools(monkeypatch)

    result = scenario.run(metadata=True)

    assert result.rows == len(values)
    assert result.metadata.input_count == len(values)
    assert scenario.loaded == [[(value,) for value in expected]]
    assert destination.rows == [(value,) for value in expected]
    assert all(row[0] is None or type(row[0]) is bool for row in scenario.loaded[0])
    assert scenario.schemas == [{"id": "BOOLEAN"}]
    assert scenario.existing_types == {"id": "BOOLEAN"}
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


@pytest.mark.parametrize("explicit_stage", [False, True])
@pytest.mark.parametrize(
    "invalid_text",
    [pytest.param("\ttrue ", id="padded"), pytest.param("private-boolean-7391", id="private")],
)
def test_invalid_authoritative_boolean_transfer_preserves_target_before_any_stage_mutation(
    monkeypatch, explicit_stage, invalid_text
):
    values = ["true", None, invalid_text]
    raw_rows = [(value,) for value in values]
    captured = capture_raw_spool_rows(monkeypatch)
    scenario, destination = boolean_target_transfer(
        monkeypatch, "VARCHAR", values, explicit_stage=explicit_stage
    )
    paths = track_spools(monkeypatch)

    with pytest.raises(TrinoBooleanValueError, match="BOOLEAN") as failure:
        scenario.run()

    message = str(failure.value)
    assert "CAST the source expression AS BOOLEAN in from_sql" in message
    assert invalid_text not in message
    assert failure.value.analytics_toolkit_sql_retry_safe is False
    assert destination.rows == _SENTINELS
    assert destination.statements == []
    assert scenario.existing_types == {"id": "BOOLEAN"}
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
