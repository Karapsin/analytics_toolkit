"""Date authority must be validated before destructive staged publication."""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.models import StageFinalizationRequest
from analytics_toolkit.sql.connection.errors import InvalidSqlInputError
from analytics_toolkit.sql.dml.transfer.flow import attempt, finalize
from sqlglot import exp, parse_one

from tests.sql._support.adaptive_transfer import install, track_spools

_SENTINELS = [(dt.date(1999, 12, 31),), (None,)]


class DateDestination:
    """Fake DBAPI boundary; real adapters generate clearing and final CAST SQL."""

    def __init__(self, backend, scenario):
        self.backend = backend
        self.scenario = scenario
        self.rows = list(_SENTINELS)
        self.statements = []

    def cursor(self):
        return DateCursor(self)

    def commit(self):
        pass

    def rollback(self):
        pass


class DateCursor:
    description = None
    rowcount = -1

    def __init__(self, destination):
        self.destination = destination

    def execute(self, statement):
        target = self.destination
        target.statements.append(statement)
        target.scenario.events.append("destination_statement")
        parsed = parse_one(statement, read=get_backend_adapter(target.backend).sqlglot_dialect)
        if isinstance(parsed, (exp.Delete, exp.TruncateTable)):
            target.rows.clear()
        elif isinstance(parsed, exp.Insert):
            converted = []
            for row in target.scenario.loaded[-1]:
                value = row[0]
                if isinstance(value, str):
                    try:
                        value = dt.date.fromisoformat(value)
                    except ValueError as error:
                        message = "Destination DATE cast rejected malformed text."
                        raise ValueError(message) from error
                converted.append((value,))
            target.rows[:] = converted
            self.rowcount = len(converted)

    def close(self):
        pass


def date_target_transfer(monkeypatch, backend, native_type, values, stage_type):
    scenario = install(
        monkeypatch,
        native_type=native_type,
        rows=[(value,) for value in values],
        from_db_key=backend,
        from_db_backend=backend,
        to_db_key=backend,
        to_db_backend=backend,
        table_schema={"id": stage_type},
        write_mode="truncate_insert",
        replace_target_table=True,
    )
    scenario.existing_types = {"id": "DATE"}
    destination = DateDestination(backend, scenario)

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
                insert_column_types={"id": "DATE"},
                write_mode=options.write_mode,
            )
        )

    monkeypatch.setattr(attempt, "finalize_loaded_stage", publish)
    monkeypatch.setattr(finalize, "finalize_loaded_stage", publish)
    return scenario, destination


@pytest.mark.parametrize("backend", ["trino", "gp"])
@pytest.mark.parametrize(
    "invalid_text",
    [
        pytest.param("not-a-date", id="malformed"),
        pytest.param("2023-02-29", id="nonleap-day"),
        pytest.param("0000-01-01", id="calendar-bound"),
        pytest.param("01/02/2024", id="ambiguous"),
        pytest.param("2024-2-29", id="noncanonical"),
        pytest.param("\uff12\uff10\uff12\uff14-\uff10\uff12-\uff12\uff19", id="unicode-digits"),
    ],
)
def test_varchar_stage_cannot_clear_existing_date_target_before_date_validation(
    monkeypatch, backend, invalid_text
):
    scenario, destination = date_target_transfer(
        monkeypatch, backend, "VARCHAR", ["2024-01-01", None, invalid_text], "VARCHAR"
    )
    paths = track_spools(monkeypatch)

    with pytest.raises(InvalidSqlInputError, match="canonical YYYY-MM-DD") as failure:
        scenario.run()

    message = str(failure.value)
    assert "CAST the source expression AS DATE in from_sql" in message
    assert invalid_text not in message
    assert "2024-01-01" not in message

    assert destination.rows == _SENTINELS
    assert destination.statements == []
    assert scenario.existing_types == {"id": "DATE"}
    assert "create_target" not in scenario.events
    assert not scenario.loaded
    assert scenario.finalizations == 0
    assert scenario.read_count == 1
    assert "complete:SELECT id FROM source" in scenario.events
    assert paths
    assert all(not path.exists() for path in paths)


@pytest.mark.parametrize("backend", ["trino", "gp"])
def test_existing_date_target_keeps_typed_rows_and_single_source_scan(monkeypatch, backend):
    values = [dt.date(2024, 1, 1), dt.date(2024, 2, 29), None]
    scenario, destination = date_target_transfer(monkeypatch, backend, "DATE", values, "DATE")
    assert scenario.run() == len(values)

    assert destination.rows == [(value,) for value in values]
    assert scenario.schemas == [{"id": "DATE"}]
    assert scenario.read_count == 1
    assert scenario.finalizations == 1
    assert scenario.events.index("complete:SELECT id FROM source") < scenario.events.index(
        "destination_statement"
    )


@pytest.mark.parametrize("backend", ["trino", "gp"])
def test_valid_iso_date_text_stage_keeps_existing_date_cast_semantics(monkeypatch, backend):
    values = ["0001-01-01", "2024-02-29", "9999-12-31", None]
    expected_stage_rows = [(value,) for value in values]
    scenario, destination = date_target_transfer(monkeypatch, backend, "VARCHAR", values, "VARCHAR")

    assert scenario.run() == len(values)

    assert destination.rows == [
        (None if value is None else dt.date.fromisoformat(value),) for value in values
    ]
    assert scenario.loaded == [expected_stage_rows]
    assert values == ["0001-01-01", "2024-02-29", "9999-12-31", None]
    assert scenario.schemas == [{"id": "VARCHAR"}]
    assert scenario.existing_types == {"id": "DATE"}
    assert scenario.read_count == 1
    assert scenario.finalizations == 1
