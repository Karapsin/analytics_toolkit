"""Finite native numeric cast views preserve authoritative text checkpoints."""

from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal

import pytest
from analytics_toolkit.sql.backends.ch.numeric_text import ClickHouseNumericTextValueError
from analytics_toolkit.sql.backends.ch.numeric_text import (
    convert_numeric_text as direct_ch_numeric_text,
)
from analytics_toolkit.sql.backends.explicit_numeric_text_cast import (
    explicit_text_numeric_validation,
)
from analytics_toolkit.sql.backends.gp.numeric_text import GreenplumNumericTextValueError
from analytics_toolkit.sql.backends.models import SourceColumn
from analytics_toolkit.sql.backends.trino.numeric_text import TrinoNumericTextValueError
from analytics_toolkit.sql.backends.type_resolution import ColumnProfile, ResolvedColumn
from analytics_toolkit.sql.connection.errors import InvalidSqlInputError
from analytics_toolkit.sql.core.replay_spool import ReplaySpool
from analytics_toolkit.sql.dml.transfer.flow.conversion_validation import (
    resolve_final_conversions,
    validate_final_conversions,
)

_EXACT = "12345678901234567890.123456789"
_CASES = [
    ("gp", "TEXT", "REAL", "float"),
    ("gp", "VARCHAR(64)", "DOUBLE PRECISION", "float"),
    ("gp", "character varying(64)", "NUMERIC(38,9)", "decimal"),
    ("trino", "VARCHAR(64)", "REAL", "float"),
    ("trino", "VARCHAR(64)", "DOUBLE", "float"),
    ("trino", "VARCHAR(64)", "DECIMAL(38,9)", "decimal"),
    ("ch", "String", "Float64", "float"),
    ("ch", "Nullable(String)", "Float64", "float"),
    ("ch", "Nullable(String)", "Nullable(Float32)", "float"),
    ("ch", "Nullable(String)", "Nullable(Decimal(38,9))", "decimal"),
]
_ERRORS = {
    "gp": GreenplumNumericTextValueError,
    "trino": TrinoNumericTextValueError,
    "ch": ClickHouseNumericTextValueError,
}


def stage_resolution(backend, native_type, **options):
    configuration = {"reason": "authoritative", "certified_input_count": 4, **options}
    return ResolvedColumn(
        target_type=native_type,
        source_type="VARCHAR",
        source_backend=backend,
        target_backend=backend,
        **configuration,
    )


@pytest.mark.parametrize(("backend", "stage_type", "target", "family"), _CASES)
def test_numeric_cast_view_preserves_text_authority_and_exact_finite_domain(
    backend, stage_type, target, family
):
    if family == "float":
        values = ["1.25", "-2.5", "1E2"]
        expected = [1.25, -2.5, 100.0]
        value_type = float
    else:
        values = [_EXACT, "-0.000000001", "1E2"]
        expected = [Decimal(_EXACT), Decimal("-0.000000001"), Decimal("1E2")]
        value_type = Decimal
    if backend != "ch" or target.startswith("Nullable("):
        values.append(None)
        expected.append(None)
    stage = stage_resolution(backend, stage_type, certified_input_count=len(values))
    before = asdict(stage)

    validator = explicit_text_numeric_validation(stage, target, backend)

    assert isinstance(validator, ResolvedColumn)
    assert validator is not stage
    assert validator.target_type == target
    assert validator.source_type == stage_type
    assert validator.source_backend == backend
    assert validator.target_backend == backend
    assert validator.certified_input_count == len(values)
    converted = [validator.convert(value) for value in values]
    assert converted == expected
    assert all(value is None or type(value) is value_type for value in converted)
    assert [stage.convert(value) for value in values] == values
    assert asdict(stage) == before


@pytest.mark.parametrize("backend", ["gp", "trino", "ch"])
@pytest.mark.parametrize(
    ("reason", "fallback"),
    [
        ("complete value profile", False),
        ("no exact native representation", True),
        ("authoritative", True),
    ],
)
def test_numeric_cast_view_never_adopts_inferred_or_fallback_text(backend, reason, fallback):
    native = "Nullable(String)" if backend == "ch" else "VARCHAR"
    target = "Nullable(Float64)" if backend == "ch" else "DOUBLE PRECISION"
    stage = stage_resolution(backend, native, reason=reason, text_fallback=fallback)
    before = asdict(stage)

    assert explicit_text_numeric_validation(stage, target, backend) is None

    assert asdict(stage) == before


@pytest.mark.parametrize(
    ("backend", "stage_type", "target"),
    [
        ("unknown", "VARCHAR", "DOUBLE"),
        ("gp", "CHAR(32)", "NUMERIC(38,9)"),
        ("trino", "CHAR(32)", "DOUBLE"),
        ("ch", "LowCardinality(String)", "Float64"),
        ("ch", "Nullable(Nullable(String))", "Float64"),
        ("ch", "String(32)", "Float64"),
        ("ch", "Array(String)", "Float64"),
        ("gp", "BIGINT", "NUMERIC(38,9)"),
        ("trino", "VARBINARY", "DOUBLE"),
        ("gp", "TEXT", "BOOLEAN"),
        ("trino", "VARCHAR", "DATE"),
        ("ch", "String", "Int64"),
        ("gp", "TEXT", "NUMERIC(38,9)[]"),
        ("trino", "VARCHAR", "ARRAY(DOUBLE)"),
        ("ch", "String", "Array(Float64)"),
        ("gp", "TEXT", '"sandbox"."custom_numeric"'),
    ],
)
def test_numeric_cast_view_leaves_other_declarations_to_existing_validation(
    backend, stage_type, target
):
    stage = stage_resolution(backend, stage_type)

    assert explicit_text_numeric_validation(stage, target, backend) is None


@pytest.mark.parametrize("backend", ["gp", "trino", "ch"])
@pytest.mark.parametrize("value", [True, 42, 1.25, Decimal("1.25"), b"1.25", {"private": 1}])
def test_numeric_cast_view_requires_actual_intermediate_text(backend, value):
    native = "Nullable(String)" if backend == "ch" else "VARCHAR"
    target = "Nullable(Float64)" if backend == "ch" else "DOUBLE PRECISION"
    validator = explicit_text_numeric_validation(stage_resolution(backend, native), target, backend)
    assert isinstance(validator, ResolvedColumn)

    with pytest.raises(InvalidSqlInputError, match=r"CAST.*numeric") as failure:
        validator.convert(value)

    assert "from_sql" in str(failure.value)
    assert "private" not in str(failure.value)


@pytest.mark.parametrize("target", ["Float64", "Decimal(38,9)"])
def test_numeric_cast_view_enforces_encoded_ch_target_nullability(target):
    stage = stage_resolution("ch", "Nullable(String)")
    validator = explicit_text_numeric_validation(stage, target, "ch")
    assert isinstance(validator, ResolvedColumn)

    assert validator.convert("1.25") == (1.25 if target == "Float64" else Decimal("1.25"))
    with pytest.raises(ValueError, match=r"NULL.*nonnullable"):
        validator.convert(None)


@pytest.mark.parametrize(
    ("backend", "target", "value"),
    [
        ("gp", "REAL", "1E100"),
        ("gp", "DOUBLE PRECISION", "1E309"),
        ("gp", "NUMERIC(38,9)", "1E29"),
        ("gp", "NUMERIC(38,9)", "0.0000000001"),
        ("trino", "REAL", "1E100"),
        ("trino", "DOUBLE", "1E309"),
        ("trino", "DECIMAL(38,9)", "1E29"),
        ("trino", "DECIMAL(38,9)", "0.0000000001"),
        ("trino", "DECIMAL(38,9)", " 1.25 "),
        ("ch", "Float32", "1E100"),
        ("ch", "Nullable(Float64)", "1E309"),
        ("ch", "Nullable(Float64)", "1E-999"),
        ("ch", "Nullable(Decimal(38,9))", "1E29"),
        ("ch", "Nullable(Decimal(38,9))", "0.0000000001"),
    ],
)
def test_numeric_cast_view_preserves_native_range_scale_and_syntax_rejections(
    backend, target, value
):
    native = "Nullable(String)" if backend == "ch" else "VARCHAR"
    validator = explicit_text_numeric_validation(stage_resolution(backend, native), target, backend)
    assert isinstance(validator, ResolvedColumn)

    with pytest.raises(_ERRORS[backend]) as failure:
        validator.convert(value)

    assert failure.value.analytics_toolkit_sql_retry_safe is False
    assert "CAST" in str(failure.value)
    assert "from_sql" in str(failure.value)


@pytest.mark.parametrize(("backend", "stage_type", "target", "family"), _CASES)
@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", "private-numeric-cast"])
def test_numeric_cast_view_rejects_nonfinite_and_private_strings_before_null_handling(
    backend, stage_type, target, family, value
):
    del family
    validator = explicit_text_numeric_validation(
        stage_resolution(backend, stage_type), target, backend
    )
    assert isinstance(validator, ResolvedColumn)

    with pytest.raises(_ERRORS[backend]) as failure:
        validator.convert(value)

    assert value not in str(failure.value)
    assert failure.value.analytics_toolkit_sql_retry_safe is False


@pytest.mark.parametrize(
    ("value", "expected"),
    [(" 1.25 ", 1.25), ("1_2.5", 12.5), ("\uff11.\uff12\uff15", 1.25), ("1E-999", 0.0)],
)
def test_ch_numeric_stage_uses_native_cast_grammar_instead_of_direct_python(value, expected):
    handled, direct = direct_ch_numeric_text(value, "Float64")
    assert handled
    assert direct == expected
    stage = stage_resolution("ch", "Nullable(String)")
    validator = explicit_text_numeric_validation(stage, "Nullable(Float64)", "ch")
    assert isinstance(validator, ResolvedColumn)

    with pytest.raises(ClickHouseNumericTextValueError):
        validator.convert(value)

    assert stage.convert(value) == value


@pytest.mark.parametrize(
    ("backend", "stage_type", "target", "value", "expected"),
    [
        ("gp", "TEXT", "DOUBLE PRECISION", " \t.5E1\r", 5.0),
        ("trino", "VARCHAR", "DOUBLE", "0x1.4p1f", 2.5),
        ("trino", "VARCHAR", "DECIMAL(38,9)", "\u0661.\u0662\u0665", Decimal("1.25")),
    ],
)
def test_numeric_cast_view_retains_each_backends_verified_native_syntax(
    backend, stage_type, target, value, expected
):
    stage = stage_resolution(backend, stage_type)
    validator = explicit_text_numeric_validation(stage, target, backend)
    assert isinstance(validator, ResolvedColumn)

    assert validator.convert(value) == expected
    assert stage.convert(value) == value


@pytest.mark.parametrize(
    ("backend", "stage_type", "target"),
    [
        ("gp", "TEXT", "NUMERIC(38,9)"),
        ("trino", "VARCHAR", "DOUBLE"),
        ("ch", "Nullable(String)", "Nullable(Decimal(38,9))"),
    ],
)
@pytest.mark.parametrize("invalid_last", [False, True])
def test_numeric_cast_view_checks_real_frozen_input_without_profile_or_stage_rewrites(
    backend, stage_type, target, invalid_last
):
    rows = [("1.25",), (None,), ("-2.5",)]
    if invalid_last:
        rows.append(("private-late-numeric",))
    stage = stage_resolution(backend, stage_type, certified_input_count=len(rows))
    before_stage = asdict(stage)
    profile = ColumnProfile(native_type=stage_type, source_backend=backend)
    for row in rows:
        profile.add(row[0])
    before_profile = asdict(profile)
    validators = resolve_final_conversions(
        [SourceColumn("id", stage_type)],
        [stage],
        [profile],
        backend=backend,
        existing_types={"id": target},
        destination_timezone=None,
    )
    assert validators is not None

    with ReplaySpool(["id"]) as spool:
        spool.write_rows(rows)
        spool.certify()
        digest = spool.digest
        if invalid_last:
            with pytest.raises(ValueError, match="resolved destination schema") as failure:
                validate_final_conversions(spool, [stage], validators)
            assert isinstance(failure.value.__cause__, _ERRORS[backend])
            assert "private-late-numeric" not in str(failure.value.__cause__)
        else:
            validate_final_conversions(spool, [stage], validators)
        assert spool.certified
        assert spool.row_count == len(rows)
        assert spool.digest == digest
        assert list(spool.iter_rows()) == rows
        assert asdict(stage) == before_stage
        assert asdict(profile) == before_profile
        path = spool.path
    assert not path.exists()
