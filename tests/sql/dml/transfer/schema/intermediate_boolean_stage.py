"""Explicit text stages retain text while final BOOLEAN casts are prevalidated."""

from __future__ import annotations

import pandas as pd
import pytest
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.gp.boolean_values import GreenplumBooleanValueError
from analytics_toolkit.sql.backends.models import StageFinalizationRequest
from analytics_toolkit.sql.backends.trino.boolean_values import TrinoBooleanValueError
from analytics_toolkit.sql.dml.transfer.flow import attempt, finalize
from sqlglot import exp, parse_one

from tests.sql._support.adaptive_transfer import install, track_spools
from tests.sql.dml.transfer.schema.direct_date_stage import capture_raw_spool_rows

_SENTINELS = [(True,), (False,), (None,)]
_STAGES = [
    pytest.param("gp", "TEXT", id="gp-text"),
    pytest.param("gp", "VARCHAR", id="gp-varchar"),
    pytest.param("trino", "VARCHAR", id="trino-varchar"),
]


class BooleanCastDestination:
    def __init__(self, backend, scenario, values, expected):
        self.backend = backend
        self.scenario = scenario
        self.expected_casts = dict(zip(values, expected))
        self.rows = list(_SENTINELS)
        self.statements = []

    def cursor(self):
        return BooleanCastCursor(self)

    def commit(self):
        pass

    def rollback(self):
        pass


class BooleanCastCursor:
    description = None
    rowcount = -1

    def __init__(self, destination):
        self.destination = destination

    def execute(self, statement):
        target = self.destination
        target.statements.append(statement)
        parsed = parse_one(statement, read=get_backend_adapter(target.backend).sqlglot_dialect)
        if isinstance(parsed, (exp.Delete, exp.TruncateTable)):
            target.rows.clear()
        elif isinstance(parsed, exp.Insert):
            cast_types = [cast.args["to"] for cast in parsed.find_all(exp.Cast)]
            assert any(native.is_type(exp.DataType.Type.BOOLEAN) for native in cast_types)
            converted = []
            for row in target.scenario.loaded[-1]:
                value = row[0]
                if value not in target.expected_casts:
                    message = "Destination BOOLEAN cast rejected unexpected staged text."
                    raise ValueError(message)
                converted.append((target.expected_casts[value],))
            target.rows[:] = converted
            self.rowcount = len(converted)

    def close(self):
        pass


def intermediate_boolean_transfer(monkeypatch, backend, stage_type, values, expected):
    scenario = install(
        monkeypatch,
        native_type="VARCHAR",
        rows=[(value,) for value in values],
        from_db_key="trino",
        from_db_backend="trino",
        to_db_key=backend,
        to_db_backend=backend,
        table_schema={"id": stage_type},
        write_mode="truncate_insert",
        replace_target_table=True,
    )
    scenario.existing_types = {"id": "BOOLEAN"}
    destination = BooleanCastDestination(backend, scenario, values, expected)

    def publish(options, connection_refs, stage_state, total_rows, **_kwargs):
        del connection_refs, stage_state, total_rows
        scenario.finalizations += 1
        get_backend_adapter(backend).finalize_stage_table(
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


@pytest.mark.parametrize(("backend", "stage_type"), _STAGES)
def test_explicit_text_boolean_stage_preserves_strings_and_validates_final_cast(
    monkeypatch, backend, stage_type
):
    if backend == "gp":
        values = ["false", "true", "of", "ye", " \tOFF\r\n", "\vYES\f", None]
    else:
        values = ["false", "TRUE", "f", "t", "0", "1", None]
    expected = [False, True, False, True, False, True, None]
    raw_rows = [(value,) for value in values]
    captured = capture_raw_spool_rows(monkeypatch)
    scenario, destination = intermediate_boolean_transfer(
        monkeypatch, backend, stage_type, values, expected
    )
    paths = track_spools(monkeypatch)

    result = scenario.run(metadata=True)

    assert result.rows == len(values)
    assert result.metadata.input_count == len(values)
    assert scenario.schemas == [{"id": stage_type}]
    assert scenario.existing_types == {"id": "BOOLEAN"}
    assert scenario.loaded == [raw_rows]
    assert all(row[0] is None or type(row[0]) is str for row in scenario.loaded[0])
    assert destination.rows == [(value,) for value in expected]
    assert all(row[0] is None or type(row[0]) is bool for row in destination.rows)
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


@pytest.mark.parametrize(("backend", "stage_type"), _STAGES)
def test_explicit_text_boolean_stage_rejects_late_private_input_before_clearing(
    monkeypatch, backend, stage_type
):
    private_text = "truex-private-9731"
    values = ["false", "true", None, private_text]
    expected = [False, True, None]
    raw_rows = [(value,) for value in values]
    captured = capture_raw_spool_rows(monkeypatch)
    scenario, destination = intermediate_boolean_transfer(
        monkeypatch, backend, stage_type, values, expected
    )
    paths = track_spools(monkeypatch)

    with pytest.raises(ValueError, match=r"(?i)boolean|destination schema") as failure:
        scenario.run()

    chain = []
    error = failure.value
    while error is not None:
        chain.append(error)
        error = error.__cause__ or error.__context__
    parser_error = GreenplumBooleanValueError if backend == "gp" else TrinoBooleanValueError
    assert any(isinstance(error, parser_error) for error in chain)
    guidance = "CAST the source expression AS BOOLEAN in from_sql"
    assert any(guidance in str(error) for error in chain)
    assert all(private_text not in str(error) for error in chain)
    assert destination.rows == _SENTINELS
    assert destination.statements == []
    assert scenario.existing_types == {"id": "BOOLEAN"}
    assert "create_target" not in scenario.events
    assert not scenario.loaded
    assert scenario.finalizations == 0
    assert scenario.read_count == 1
    assert captured == raw_rows
    assert "complete:SELECT id FROM source" in scenario.events
    assert paths
    assert all(not path.exists() for path in paths)
