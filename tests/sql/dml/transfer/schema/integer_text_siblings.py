"""Source-proven integer text casts retain width and backend grammar contracts."""

from __future__ import annotations

import pandas as pd
import pytest
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.models import StageFinalizationRequest
from analytics_toolkit.sql.dml.transfer.flow import attempt, finalize
from sqlglot import exp, parse_one

from tests.sql._support.adaptive_transfer import install, track_spools
from tests.sql.dml.transfer.schema.direct_date_stage import capture_raw_spool_rows

_WIDTHS = [
    ("gp", "SMALLINT", 16),
    ("gp", "INTEGER", 32),
    ("gp", "BIGINT", 64),
    ("trino", "TINYINT", 8),
    ("trino", "SMALLINT", 16),
    ("trino", "INTEGER", 32),
]
_CASES = [
    pytest.param(
        (backend, native, bits, stage),
        id=f"{backend}-{native.lower()}-{stage.lower()}",
    )
    for backend, native, bits in _WIDTHS
    for stage in ((native, "TEXT", "VARCHAR") if backend == "gp" else (native, "VARCHAR"))
]
_SQL_TYPES = {
    "TINYINT": exp.DataType.Type.TINYINT,
    "SMALLINT": exp.DataType.Type.SMALLINT,
    "INTEGER": exp.DataType.Type.INT,
    "BIGINT": exp.DataType.Type.BIGINT,
}
_SENTINELS = [(97,), (-97,), (None,)]


class IntegerDestination:
    def __init__(self, case, scenario, text_casts):
        self.backend, self.native, self.bits, _stage = case
        self.scenario = scenario
        self.text_casts = text_casts
        self.rows = list(_SENTINELS)
        self.statements = []

    def cursor(self):
        return IntegerCursor(self)

    def commit(self):
        pass

    def rollback(self):
        pass


class IntegerCursor:
    description = None
    rowcount = -1

    def __init__(self, destination):
        self.destination = destination

    def execute(self, statement):
        target = self.destination
        parsed = parse_one(statement, read=get_backend_adapter(target.backend).sqlglot_dialect)
        target.statements.append(statement)
        if isinstance(parsed, (exp.Delete, exp.TruncateTable)):
            target.rows.clear()
        elif isinstance(parsed, exp.Insert):
            casts = [cast.args["to"] for cast in parsed.find_all(exp.Cast)]
            assert any(native.is_type(_SQL_TYPES[target.native]) for native in casts)
            minimum, maximum = -(2 ** (target.bits - 1)), 2 ** (target.bits - 1) - 1
            converted = []
            for row in target.scenario.loaded[-1]:
                value = row[0]
                if isinstance(value, str):
                    if value not in target.text_casts:
                        message = "Destination integer CAST rejected staged text."
                        raise ValueError(message)
                    value = target.text_casts[value]
                if value is not None and (
                    type(value) is not int or not minimum <= value <= maximum
                ):
                    message = "Destination integer CAST rejected its native width."
                    raise ValueError(message)
                converted.append((value,))
            target.rows[:] = converted
            self.rowcount = len(converted)

    def close(self):
        pass


def integer_transfer(monkeypatch, case, values, text_casts):
    backend, native, _bits, stage = case
    scenario = install(
        monkeypatch,
        native_type="VARCHAR",
        rows=[(value,) for value in values],
        from_db_key="trino",
        from_db_backend="trino",
        to_db_key=backend,
        to_db_backend=backend,
        table_schema={"id": stage},
        write_mode="truncate_insert",
        replace_target_table=True,
    )
    scenario.existing_types = {"id": native}
    destination = IntegerDestination(case, scenario, text_casts)

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
                insert_column_types={"id": native},
                write_mode=options.write_mode,
            )
        )

    monkeypatch.setattr(attempt, "finalize_loaded_stage", publish)
    monkeypatch.setattr(finalize, "finalize_loaded_stage", publish)
    return scenario, destination


@pytest.mark.parametrize("case", _CASES)
def test_integer_sibling_text_casts_preserve_native_width_and_original_frozen_rows(
    monkeypatch, case
):
    backend, native, bits, stage = case
    minimum, maximum = -(2 ** (bits - 1)), 2 ** (bits - 1) - 1
    values = ["42", "-7", "0", str(minimum), str(maximum)]
    expected = [42, -7, 0, minimum, maximum]
    if backend == "gp":
        values.append(" \t+42\r\n")
        expected.append(42)
    else:
        values.extend(["\x00+42\x1f", "\u0664\u0662"])
        expected.extend([42, 42])
    values.append(None)
    expected.append(None)
    raw_rows = [(value,) for value in values]
    captured = capture_raw_spool_rows(monkeypatch)
    scenario, destination = integer_transfer(monkeypatch, case, values, dict(zip(values, expected)))
    paths = track_spools(monkeypatch)

    result = scenario.run(metadata=True)

    assert result.rows == len(values)
    assert result.metadata.input_count == len(values)
    assert scenario.schemas == [{"id": stage}]
    assert scenario.existing_types == {"id": native}
    textual = stage in {"TEXT", "VARCHAR"}
    stage_values = values if textual else expected
    stage_class = str if textual else int
    assert scenario.loaded == [[(value,) for value in stage_values]]
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


@pytest.mark.parametrize("case", _CASES)
@pytest.mark.parametrize("invalid_kind", ["malformed-private", "overflow"])
def test_integer_sibling_rejects_late_invalid_text_before_any_destination_mutation(
    monkeypatch, case, invalid_kind
):
    _backend, native, bits, _stage = case
    invalid_text = (
        "private-integer-7391" if invalid_kind == "malformed-private" else str(2 ** (bits - 1))
    )
    values = ["42", None, invalid_text]
    raw_rows = [(value,) for value in values]
    captured = capture_raw_spool_rows(monkeypatch)
    scenario, destination = integer_transfer(monkeypatch, case, values, {"42": 42, None: None})
    paths = track_spools(monkeypatch)

    with pytest.raises(
        ValueError, match=r"(?i)integer|TINYINT|SMALLINT|BIGINT|destination schema"
    ) as failure:
        scenario.run()

    if invalid_kind == "malformed-private":
        error = failure.value
        while error is not None:
            assert invalid_text not in str(error)
            error = error.__cause__ or (None if error.__suppress_context__ else error.__context__)
    assert destination.rows == _SENTINELS
    assert destination.statements == []
    assert scenario.existing_types == {"id": native}
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
