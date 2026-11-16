"""Final finite numeric casts validate complete text stages without rewriting them."""

from __future__ import annotations

import math
import re
from decimal import Decimal, localcontext

import pytest
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.dml.transfer.flow import attempt, finalize
from sqlglot import exp, parse_one

from tests.sql._support.adaptive_transfer import install, track_spools
from tests.sql.dml.transfer.schema.direct_date_stage import capture_raw_spool_rows

_EXACT = "12345678901234567890.123456789"
_PORTABLE_TEXT = re.compile(r"[+-]?[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]{1,3})?", re.ASCII)
_CONFIGURATIONS = [
    ("gp", "TEXT", "DOUBLE PRECISION", "float"),
    ("gp", "VARCHAR", "DOUBLE PRECISION", "float"),
    ("gp", "TEXT", "NUMERIC(38,9)", "decimal"),
    ("gp", "VARCHAR", "NUMERIC(38,9)", "decimal"),
    ("trino", "VARCHAR", "DOUBLE", "float"),
    ("trino", "VARCHAR", "DECIMAL(38,9)", "decimal"),
    ("ch", "Nullable(String)", "Nullable(Float64)", "float"),
    ("ch", "Nullable(String)", "Nullable(Decimal(38,9))", "decimal"),
]
_CASES = [
    pytest.param(case, id=f"{case[0]}-{case[1].lower()}-{case[3]}") for case in _CONFIGURATIONS
]
_INVALID = [
    pytest.param(case, kind, value, id=f"{case[0]}-{case[1].lower()}-{case[3]}-{kind}")
    for case in _CONFIGURATIONS
    for kind, value in (
        [
            ("private", "private-numeric-6821"),
            ("nan", "NaN"),
            ("infinity", "Infinity"),
            ("range", "1E309" if case[3] == "float" else "1E29"),
        ]
        + ([("scale", "0.0000000001")] if case[3] == "decimal" else [])
        + (
            [("native-whitespace", " 1.25 "), ("native-underscores", "1_2.5")]
            if case[0] == "ch"
            else []
        )
        + ([("native-underflow", "1E-999")] if case[0] == "ch" and case[3] == "float" else [])
    )
]


def portable_numeric_cast(value, family):
    if value is None:
        return None
    if not isinstance(value, str) or _PORTABLE_TEXT.fullmatch(value) is None:
        message = "Independent destination numeric CAST rejected staged text."
        raise ValueError(message)
    if family == "float":
        number = float(value)
        if not math.isfinite(number) or (number == 0 and Decimal(value) != 0):
            message = "Independent destination float CAST rejected its finite range."
            raise ValueError(message)
        return number
    with localcontext() as context:
        context.prec = 100
        original = Decimal(value)
        number = original.quantize(Decimal("0.000000001"))
        if number != original or original.copy_abs() >= Decimal("1E29"):
            message = "Independent destination decimal CAST rejected precision or scale."
            raise ValueError(message)
        return number


class NumericDestination:
    """Fake server accepts only independently checked portable canonical casts."""

    def __init__(self, case, scenario):
        self.backend, self.stage_type, self.native_type, self.family = case
        self.scenario = scenario
        self.sentinels = [(Decimal("99.123456789") if self.family == "decimal" else 99.5,), (None,)]
        self.rows = list(self.sentinels)
        self.statements = []
        self.rowcount = -1

    def cursor(self):
        return NumericCursor(self)

    def commit(self):
        pass

    def rollback(self):
        pass

    def command(self, statement, **_kwargs):
        self.apply(statement)

    def apply(self, statement):
        self.statements.append(statement)
        parsed = parse_one(statement, read=get_backend_adapter(self.backend).sqlglot_dialect)
        if isinstance(parsed, (exp.Delete, exp.TruncateTable)):
            self.rows.clear()
        elif isinstance(parsed, exp.Insert):
            assert list(parsed.find_all(exp.Cast))
            converted = [
                (portable_numeric_cast(row[0], self.family),) for row in self.scenario.loaded[-1]
            ]
            self.rows[:] = converted
            self.rowcount = len(converted)


class NumericCursor:
    description = None

    def __init__(self, destination):
        self.destination = destination
        self.rowcount = -1

    def execute(self, statement):
        self.destination.apply(statement)
        self.rowcount = self.destination.rowcount

    def close(self):
        pass


def numeric_text_transfer(monkeypatch, case, values):
    backend, stage_type, native_type, _family = case
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
    scenario.existing_types = {"id": native_type}
    destination = NumericDestination(case, scenario)

    def publish(options, connection_refs, stage_state, total_rows, **_kwargs):
        del connection_refs, stage_state, total_rows
        scenario.finalizations += 1
        adapter = get_backend_adapter(backend)
        for statement in adapter.clear_table_sqls(options.target_table):
            adapter.execute_command(destination, statement)
        adapter.insert_from_table(
            destination,
            options.target_table,
            "sandbox.incoming_stage",
            column_types={"id": native_type},
        )

    monkeypatch.setattr(attempt, "finalize_loaded_stage", publish)
    monkeypatch.setattr(finalize, "finalize_loaded_stage", publish)
    return scenario, destination


@pytest.mark.parametrize("case", _CASES)
def test_numeric_text_stage_preserves_raw_strings_and_exact_finite_final_values(monkeypatch, case):
    if case[3] == "decimal":
        values = [None, _EXACT, "-0.000000001", "1E2", None]
        expected = [None, Decimal(_EXACT), Decimal("-0.000000001"), Decimal("100.000000000"), None]
        value_class = Decimal
    else:
        values = [None, "1.25", "-2.5", "1E2", None]
        expected = [None, 1.25, -2.5, 100.0, None]
        value_class = float
    raw_rows = [(value,) for value in values]
    captured = capture_raw_spool_rows(monkeypatch)
    scenario, destination = numeric_text_transfer(monkeypatch, case, values)
    paths = track_spools(monkeypatch)

    result = scenario.run(metadata=True)

    assert result.rows == len(values)
    assert result.metadata.input_count == len(values)
    assert scenario.schemas == [{"id": case[1]}]
    assert scenario.existing_types == {"id": case[2]}
    assert scenario.loaded == [raw_rows]
    assert all(row[0] is None or type(row[0]) is str for row in scenario.loaded[0])
    assert destination.rows == [(value,) for value in expected]
    assert all(row[0] is None or type(row[0]) is value_class for row in destination.rows)
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


@pytest.mark.parametrize(("case", "invalid_kind", "invalid_text"), _INVALID)
def test_numeric_text_stage_rejects_unsafe_last_cast_before_any_mutation(
    monkeypatch, case, invalid_kind, invalid_text
):
    values = ["1.25", None, invalid_text]
    raw_rows = [(value,) for value in values]
    captured = capture_raw_spool_rows(monkeypatch)
    scenario, destination = numeric_text_transfer(monkeypatch, case, values)
    paths = track_spools(monkeypatch)

    with pytest.raises(
        ValueError, match=r"(?i)numeric|float|decimal|destination schema"
    ) as failure:
        scenario.run()

    if invalid_kind == "private":
        error = failure.value
        while error is not None:
            assert invalid_text not in str(error)
            error = error.__cause__ or (None if error.__suppress_context__ else error.__context__)
    assert destination.rows == destination.sentinels
    assert destination.statements == []
    assert scenario.existing_types == {"id": case[2]}
    assert "create_target" not in scenario.events
    assert not scenario.loaded
    assert scenario.finalizations == 0
    assert scenario.read_count == 1
    assert captured == raw_rows
    assert "complete:SELECT id FROM source" in scenario.events
    assert paths
    assert all(not path.exists() for path in paths)


@pytest.mark.parametrize(
    ("native_type", "family"), [("Float64", "float"), ("Decimal(38,9)", "decimal")]
)
def test_ch_numeric_text_stage_preserves_encoded_nonnullable_target_constraint(
    monkeypatch, native_type, family
):
    case = ("ch", "Nullable(String)", native_type, family)
    values = ["1.25", None]
    captured = capture_raw_spool_rows(monkeypatch)
    scenario, destination = numeric_text_transfer(monkeypatch, case, values)
    destination.sentinels = destination.sentinels[:1]
    destination.rows = list(destination.sentinels)
    paths = track_spools(monkeypatch)

    with pytest.raises(ValueError, match="destination schema") as failure:
        scenario.run()

    chain = []
    error = failure.value
    while error is not None:
        chain.append(error)
        error = error.__cause__ or (None if error.__suppress_context__ else error.__context__)
    assert any("nonnullable authoritative column" in str(error) for error in chain)
    assert destination.rows == destination.sentinels
    assert destination.statements == []
    assert scenario.existing_types == {"id": native_type}
    assert "create_target" not in scenario.events
    assert not scenario.loaded
    assert scenario.finalizations == 0
    assert scenario.read_count == 1
    assert captured == [(value,) for value in values]
    assert paths
    assert all(not path.exists() for path in paths)
