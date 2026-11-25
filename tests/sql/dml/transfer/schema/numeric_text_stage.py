"""Native numeric casts validate complete text stages without rewriting them."""

from __future__ import annotations

import math
import re
from decimal import Decimal, localcontext

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.dml.transfer.flow import attempt, finalize
from sqlglot import exp, parse_one

from tests.sql._support.adaptive_transfer import install, track_spools
from tests.sql._support.numeric_text_transfer import install_numeric_text_transfer
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
    if not (
        kind in {"nan", "infinity"}
        and (case[0] == "gp" or (case[0] == "trino" and case[3] == "float"))
    )
]


def portable_numeric_cast(value, family, backend):
    if value is None:
        return None
    # Trino's VARCHAR-to-DOUBLE operator delegates these tokens to parseDouble.
    # Its native NaN/Infinity values are distinct from SQL NULL.
    if backend == "trino" and family == "float" and value in {"NaN", "Infinity", "-Infinity"}:
        return float(value)
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
                (portable_numeric_cast(row[0], self.family, self.backend),)
                for row in self.scenario.loaded[-1]
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


@pytest.mark.parametrize("text", ["NaN", "Infinity", "-Infinity"])
def test_trino_float_text_stage_preserves_native_specials_and_distinct_null(monkeypatch, text):
    values = ["1.25", None, text]
    raw_rows = [(value,) for value in values]
    captured = capture_raw_spool_rows(monkeypatch)
    case = ("trino", "VARCHAR", "DOUBLE", "float")
    scenario, destination = numeric_text_transfer(monkeypatch, case, values)
    paths = track_spools(monkeypatch)

    result = scenario.run(metadata=True)

    assert result.rows == result.metadata.input_count == 3
    assert destination.rows[:2] == [(1.25,), (None,)]
    special = destination.rows[2][0]
    assert type(special) is float
    if text == "NaN":
        assert math.isnan(special)
    else:
        assert special == float(text)
    assert scenario.schemas == [{"id": "VARCHAR"}]
    assert scenario.existing_types == {"id": "DOUBLE"}
    assert scenario.loaded == [raw_rows]
    assert captured == raw_rows
    assert scenario.read_count == scenario.finalizations == 1
    assert scenario.events.index("complete:SELECT id FROM source") < scenario.events.index(
        "create_target"
    )
    assert paths
    assert all(not path.exists() for path in paths)


def _run_gp_text_stage(stage_type):
    return sql.transfer(
        "trino",
        "gp",
        from_sql="SELECT id FROM source",
        to_table="sandbox.events",
        table_schema={"id": stage_type},
        write_mode="truncate_insert",
        ignore_source_staging=True,
        validate_row_count=False,
        retry_cnt=1,
        full_retry_cnt=1,
        timeout_increment=0,
        full_timeout_increment=0,
        return_metadata=True,
    )


@pytest.mark.parametrize("stage_type", ["TEXT", "VARCHAR"])
@pytest.mark.parametrize(
    ("native_type", "text"),
    [
        ("DOUBLE PRECISION", "NaN"),
        ("DOUBLE PRECISION", "Infinity"),
        ("DOUBLE PRECISION", "-Infinity"),
        ("NUMERIC(38,9)", "NaN"),
    ],
)
def test_gp_numeric_text_stage_certifies_native_specials_and_distinct_null(
    monkeypatch, stage_type, native_type, text
):
    raw_rows = [("1.25",), (None,), (text,)]
    captured = capture_raw_spool_rows(monkeypatch)
    database = install_numeric_text_transfer(
        monkeypatch, rows=raw_rows, final_type=native_type, stage_type=stage_type
    )
    paths = track_spools(monkeypatch)

    result = _run_gp_text_stage(stage_type)

    assert result.rows == result.metadata.input_count == 3
    assert result.metadata.inserted_rows == result.metadata.affected_rows == 3
    expected_finite = Decimal("1.250000000") if native_type.startswith("NUMERIC") else 1.25
    assert database.target.rows[:2] == [{"id": expected_finite}, {"id": None}]
    special = database.target.rows[2]["id"]
    if native_type.startswith("NUMERIC"):
        assert type(special) is Decimal
        assert special.is_qnan()
        assert database.private_value_attempts
        assert ("sandbox", "events") not in database.private_value_attempts
        assert result.metadata.type_resolutions[0]["native_assignment_validation"] == "certified"
        assert result.metadata.type_resolutions[0]["certified_input_count"] == 3
    else:
        assert type(special) is float
        if text == "NaN":
            assert math.isnan(special)
        else:
            assert special == float(text)
    assert database.target.schema == {"id": native_type}
    assert database.original_source_rows == raw_rows
    assert database.bound_rows == raw_rows
    assert captured == raw_rows
    assert len(database.reads) == database.source_opens == 1
    assert len(database.source_completions) == 1
    assert database.live_mutations
    assert set(database.tables) == {("sandbox", "events")}
    assert paths
    assert all(not path.exists() for path in paths)
    assert all(connection.close_count == 1 for connection in database.opened)


@pytest.mark.parametrize("stage_type", ["TEXT", "VARCHAR"])
@pytest.mark.parametrize("text", ["Infinity", "-Infinity"])
def test_gp_numeric_text_stage_rejects_unsupported_native_infinity_before_live_mutation(
    monkeypatch, stage_type, text
):
    raw_rows = [("1.25",), (None,), (text,)]
    captured = capture_raw_spool_rows(monkeypatch)
    database = install_numeric_text_transfer(
        monkeypatch, rows=raw_rows, final_type="NUMERIC(38,9)", stage_type=stage_type
    )
    paths = track_spools(monkeypatch)

    with pytest.raises(ValueError, match=r"(?i)native|numeric|destination schema") as failure:
        _run_gp_text_stage(stage_type)

    assert text not in str(failure.value)
    assert database.target.rows == database.sentinel_rows
    assert database.target.schema == {"id": "NUMERIC(38,9)"}
    assert database.live_mutations == []
    assert database.private_value_attempts
    assert ("sandbox", "events") not in database.private_value_attempts
    assert database.bound_rows == raw_rows
    assert database.original_source_rows == captured == raw_rows
    assert len(database.reads) == database.source_opens == 1
    assert len(database.source_completions) == 1
    assert set(database.tables) == {("sandbox", "events")}
    assert paths
    assert all(not path.exists() for path in paths)
    assert all(connection.close_count == 1 for connection in database.opened)


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
