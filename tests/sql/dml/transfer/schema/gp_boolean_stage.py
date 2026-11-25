"""GP BOOLEAN authority uses native text rules before any destination mutation."""

from __future__ import annotations

import pandas as pd
import pytest
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.gp.boolean_values import GreenplumBooleanValueError
from analytics_toolkit.sql.backends.models import StageFinalizationRequest
from analytics_toolkit.sql.dml.transfer.flow import attempt, finalize
from sqlglot import exp, parse_one

from tests.sql._support.adaptive_transfer import install, track_spools
from tests.sql.dml.transfer.schema.direct_date_stage import capture_raw_spool_rows

_SENTINELS = [(False,), (True,), (None,)]
_TEXT_CASES = [
    ("true", True),
    ("t", True),
    ("tr", True),
    ("tru", True),
    ("y", True),
    ("ye", True),
    ("yes", True),
    ("on", True),
    ("1", True),
    ("TrUe", True),
    (" \t\r\n\v\fYES \t\r\n\v\f", True),
    ("false", False),
    ("f", False),
    ("fa", False),
    ("fal", False),
    ("fals", False),
    ("n", False),
    ("no", False),
    ("of", False),
    ("off", False),
    ("0", False),
    ("FaLsE", False),
    ("\tOFF\r\n", False),
]


class GreenplumBooleanDestination:
    def __init__(self, scenario):
        self.scenario = scenario
        self.rows = list(_SENTINELS)
        self.statements = []

    def cursor(self):
        return GreenplumBooleanCursor(self)

    def commit(self):
        pass

    def rollback(self):
        pass


class GreenplumBooleanCursor:
    description = None
    rowcount = -1

    def __init__(self, destination):
        self.destination = destination

    def execute(self, statement):
        target = self.destination
        target.statements.append(statement)
        parsed = parse_one(statement, read=get_backend_adapter("gp").sqlglot_dialect)
        if isinstance(parsed, (exp.Delete, exp.TruncateTable)):
            target.rows.clear()
        elif isinstance(parsed, exp.Insert):
            rows = target.scenario.loaded[-1]
            if any(row[0] is not None and type(row[0]) is not bool for row in rows):
                message = "GP BOOLEAN requires normalized Boolean stage values."
                raise ValueError(message)
            target.rows[:] = rows
            self.rowcount = len(rows)

    def close(self):
        pass


def gp_boolean_transfer(monkeypatch, values, *, explicit_stage):
    scenario = install(
        monkeypatch,
        native_type="VARCHAR",
        rows=[(value,) for value in values],
        from_db_key="trino",
        from_db_backend="trino",
        to_db_key="gp",
        to_db_backend="gp",
        table_schema={"id": "BOOLEAN"} if explicit_stage else None,
        write_mode="truncate_insert",
        replace_target_table=True,
    )
    scenario.existing_types = {"id": "BOOLEAN"}
    destination = GreenplumBooleanDestination(scenario)

    def publish(options, connection_refs, stage_state, total_rows, **_kwargs):
        del connection_refs, stage_state, total_rows
        scenario.finalizations += 1
        get_backend_adapter("gp").finalize_stage_table(
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
def test_gp_boolean_transfer_accepts_native_text_prefixes_and_preserves_raw_source(
    monkeypatch, explicit_stage
):
    values = [value for value, _expected in _TEXT_CASES] + [None]
    expected = [expected for _value, expected in _TEXT_CASES] + [None]
    raw_rows = [(value,) for value in values]
    captured = capture_raw_spool_rows(monkeypatch)
    scenario, destination = gp_boolean_transfer(monkeypatch, values, explicit_stage=explicit_stage)
    paths = track_spools(monkeypatch)

    result = scenario.run(metadata=True)

    assert scenario.options.from_db_key != scenario.options.to_db_key
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
    assert scenario.source_open_count == 1
    assert scenario.finalizations == 1
    assert scenario.events.index("complete:SELECT id FROM source") < scenario.events.index(
        "create_target"
    )
    assert paths
    assert all(not path.exists() for path in paths)


@pytest.mark.parametrize("explicit_stage", [False, True])
@pytest.mark.parametrize(
    "invalid_text",
    [
        pytest.param(" \to\r\n", id="ambiguous-prefix"),
        pytest.param("truex-private-7391", id="overlong-private"),
        pytest.param("tr ue", id="interior-whitespace"),
        pytest.param("true\u00a0", id="non-ascii-whitespace"),
    ],
)
def test_gp_boolean_transfer_rejects_late_invalid_text_before_any_mutation(
    monkeypatch, explicit_stage, invalid_text
):
    values = ["true", None, invalid_text]
    raw_rows = [(value,) for value in values]
    captured = capture_raw_spool_rows(monkeypatch)
    scenario, destination = gp_boolean_transfer(monkeypatch, values, explicit_stage=explicit_stage)
    paths = track_spools(monkeypatch)

    with pytest.raises(GreenplumBooleanValueError, match="BOOLEAN") as failure:
        scenario.run()

    message = str(failure.value)
    assert "CAST the source expression AS BOOLEAN in from_sql" in message
    assert invalid_text not in message
    assert failure.value.analytics_toolkit_sql_retry_safe is False
    assert scenario.options.from_db_key != scenario.options.to_db_key
    assert destination.rows == _SENTINELS
    assert destination.statements == []
    assert scenario.existing_types == {"id": "BOOLEAN"}
    assert "create_target" not in scenario.events
    assert not scenario.loaded
    assert scenario.finalizations == 0
    assert scenario.read_count == 1
    assert scenario.source_open_count == 1
    assert len(captured) >= 2
    assert captured == raw_rows[: len(captured)]
    expected_types = [type(row[0]) for row in raw_rows[: len(captured)]]
    assert [type(row[0]) for row in captured] == expected_types
    assert paths
    assert all(not path.exists() for path in paths)
