"""Native final integer cast checks preserve explicit textual stage authority."""

from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal

import pytest
from analytics_toolkit.sql.backends.explicit_integer_cast import explicit_text_integer_validation
from analytics_toolkit.sql.backends.gp.integer_values import GreenplumIntegerValueError
from analytics_toolkit.sql.backends.models import SourceColumn
from analytics_toolkit.sql.backends.trino.integer_values import (
    TrinoIntegerValueError,
    integer_value,
)
from analytics_toolkit.sql.backends.type_resolution import ColumnProfile, ResolvedColumn
from analytics_toolkit.sql.connection.errors import InvalidSqlInputError
from analytics_toolkit.sql.core.replay_spool import ReplaySpool
from analytics_toolkit.sql.dml.transfer.flow.conversion_validation import (
    resolve_final_conversions,
    validate_final_conversions,
)

_TARGETS = [
    ("gp", "SMALLINT", 16),
    ("gp", "INTEGER", 32),
    ("gp", "BIGINT", 64),
    ("gp", '"pg_catalog"."int4"', 32),
    ("trino", "TINYINT", 8),
    ("trino", "SMALLINT", 16),
    ("trino", "INTEGER", 32),
    ("trino", "BIGINT", 64),
]


def _stage(backend, native_type="VARCHAR(64)", **options):
    configuration = {"reason": "authoritative", "certified_input_count": 6, **options}
    return ResolvedColumn(
        target_type=native_type,
        source_type="VARCHAR",
        source_backend=backend,
        target_backend=backend,
        **configuration,
    )


@pytest.mark.parametrize(("backend", "target", "bits"), _TARGETS)
def test_explicit_integer_cast_uses_native_width_and_keeps_stage_metadata(backend, target, bits):
    stage_type = "character varying(64)" if backend == "gp" else "VARCHAR(64)"
    minimum, maximum = -(2 ** (bits - 1)), 2 ** (bits - 1) - 1
    values = ["42", "-7", str(minimum), str(maximum), "\t+0007\n", None]
    expected = [42, -7, minimum, maximum, 7, None]
    if backend == "trino":
        values.extend(["\x00+42\x1f", "\u0664\u0662"])
        expected.extend([42, 42])
    stage = _stage(backend, stage_type, certified_input_count=len(values))
    before = asdict(stage)

    validator = explicit_text_integer_validation(stage, target, backend)

    assert isinstance(validator, ResolvedColumn)
    assert validator is not stage
    assert validator.target_type == target
    assert validator.source_type == stage_type
    assert validator.source_backend == backend
    assert validator.target_backend == backend
    assert validator.certified_input_count == stage.certified_input_count
    converted = [validator.convert(value) for value in values]
    assert converted == expected
    assert all(value is None or type(value) is int for value in converted)
    assert [stage.convert(value) for value in values] == values
    assert asdict(stage) == before


@pytest.mark.parametrize("backend", ["gp", "trino"])
@pytest.mark.parametrize(
    ("reason", "fallback"),
    [
        ("complete value profile", False),
        ("no exact native representation", True),
        ("authoritative", True),
    ],
)
def test_explicit_integer_cast_does_not_adopt_inferred_or_fallback_text(backend, reason, fallback):
    stage = _stage(backend, reason=reason, text_fallback=fallback)
    before = asdict(stage)

    assert explicit_text_integer_validation(stage, "BIGINT", backend) is None

    assert asdict(stage) == before


@pytest.mark.parametrize(
    ("backend", "stage_type", "target_type"),
    [
        ("ch", "String", "Int64"),
        ("gp", "CHAR(16)", "INTEGER"),
        ("trino", "CHAR(16)", "INTEGER"),
        ("trino", "TEXT", "INTEGER"),
        ("gp", "BIGINT", "SMALLINT"),
        ("trino", "BIGINT", "INTEGER"),
        ("gp", "BYTEA", "INTEGER"),
        ("trino", "VARBINARY", "INTEGER"),
        ("gp", "TEXT", "DATE"),
        ("trino", "VARCHAR", "BOOLEAN"),
        ("gp", "TEXT", "INTEGER[]"),
        ("trino", "VARCHAR", "ARRAY(INTEGER)"),
        ("gp", "TEXT", '"sandbox"."custom_integer"'),
    ],
)
def test_explicit_integer_cast_leaves_other_contexts_to_existing_validation(
    backend, stage_type, target_type
):
    stage = _stage(backend, stage_type)

    assert explicit_text_integer_validation(stage, target_type, backend) is None


@pytest.mark.parametrize("backend", ["gp", "trino"])
@pytest.mark.parametrize("value", [True, 42, Decimal(42), b"42", {"private": 1}, ("42",)])
def test_explicit_integer_cast_requires_actual_stage_text(backend, value):
    validator = explicit_text_integer_validation(_stage(backend), "INTEGER", backend)
    assert isinstance(validator, ResolvedColumn)

    with pytest.raises(InvalidSqlInputError, match=r"CAST.*integer") as failure:
        validator.convert(value)

    assert "from_sql" in str(failure.value)
    assert "private" not in str(failure.value)


@pytest.mark.parametrize("value", ["4_2", "\u00a042\u00a0", "\U0001d7dc\U0001d7da"])
def test_final_bigint_text_cast_never_uses_python_only_integer_forms(value):
    stage = _stage("trino")
    validator = explicit_text_integer_validation(stage, "BIGINT", "trino")
    assert isinstance(validator, ResolvedColumn)

    assert integer_value(value, "BIGINT") == 42
    with pytest.raises(TrinoIntegerValueError, match="integer") as failure:
        validator.convert(value)

    assert failure.value.analytics_toolkit_sql_retry_safe is False
    assert value not in str(failure.value)
    assert stage.convert(value) == value


@pytest.mark.parametrize(("backend", "target", "bits"), _TARGETS)
@pytest.mark.parametrize("invalid_kind", ["private", "underflow", "overflow"])
def test_final_integer_text_cast_preserves_native_parser_rejections(
    backend, target, bits, invalid_kind
):
    invalid = {
        "private": "private-native-integer",
        "underflow": str(-(2 ** (bits - 1)) - 1),
        "overflow": str(2 ** (bits - 1)),
    }[invalid_kind]
    validator = explicit_text_integer_validation(_stage(backend), target, backend)
    assert isinstance(validator, ResolvedColumn)
    error_type = GreenplumIntegerValueError if backend == "gp" else TrinoIntegerValueError

    with pytest.raises(error_type, match="integer") as failure:
        validator.convert(invalid)

    message = str(failure.value)
    assert "CAST" in message
    assert "from_sql" in message
    if invalid_kind == "private":
        assert invalid not in message
    assert failure.value.__cause__ is None
    assert failure.value.analytics_toolkit_sql_retry_safe is False


@pytest.mark.parametrize(("backend", "target"), [("gp", "SMALLINT"), ("trino", "BIGINT")])
@pytest.mark.parametrize("invalid_last", [False, True])
def test_final_integer_cast_checks_all_frozen_rows_without_rewriting_stage(
    backend, target, invalid_last
):
    rows = [("42",), (None,), ("-7",)]
    if invalid_last:
        rows.append(("private-late-integer",))
    stage_type = "TEXT" if backend == "gp" else "VARCHAR"
    stage = _stage(backend, stage_type, certified_input_count=len(rows))
    before = asdict(stage)
    profile = ColumnProfile(native_type=stage_type, source_backend=backend)
    for row in rows:
        profile.add(row[0])
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
            expected_error = (
                GreenplumIntegerValueError if backend == "gp" else TrinoIntegerValueError
            )
            assert isinstance(failure.value.__cause__, expected_error)
            assert "private-late-integer" not in str(failure.value.__cause__)
        else:
            validate_final_conversions(spool, [stage], validators)
        assert spool.certified
        assert spool.row_count == len(rows)
        assert spool.digest == digest
        assert list(spool.iter_rows()) == rows
        assert asdict(stage) == before
        path = spool.path
    assert not path.exists()
