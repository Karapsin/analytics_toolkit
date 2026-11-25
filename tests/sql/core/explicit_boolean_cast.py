"""Explicit BOOLEAN cast checks preserve the certified textual stage payload."""

from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal

import pytest
from analytics_toolkit.sql.backends.explicit_boolean_cast import explicit_text_boolean_validation
from analytics_toolkit.sql.backends.gp.boolean_values import GreenplumBooleanValueError
from analytics_toolkit.sql.backends.models import SourceColumn
from analytics_toolkit.sql.backends.trino.boolean_values import TrinoBooleanValueError
from analytics_toolkit.sql.backends.type_resolution import ColumnProfile, ResolvedColumn
from analytics_toolkit.sql.connection.errors import InvalidSqlInputError
from analytics_toolkit.sql.core.replay_spool import ReplaySpool
from analytics_toolkit.sql.dml.transfer.flow.conversion_validation import (
    resolve_final_conversions,
    validate_final_conversions,
)

_GP_TEXT = [
    ("t", True),
    ("tr", True),
    ("tru", True),
    ("true", True),
    ("y", True),
    ("ye", True),
    ("yes", True),
    ("on", True),
    ("1", True),
    ("f", False),
    ("fa", False),
    ("fal", False),
    ("fals", False),
    ("false", False),
    ("n", False),
    ("no", False),
    ("of", False),
    ("off", False),
    ("0", False),
    (" \tTRUE\r\n\v\f", True),
    ("\tFaLsE\r", False),
    (None, None),
]
_TRINO_TEXT = [
    ("true", True),
    ("t", True),
    ("1", True),
    ("false", False),
    ("f", False),
    ("0", False),
    ("TrUe", True),
    ("FALSE", False),
    (None, None),
]


def _stage(native_type, backend, **options):
    configuration = {"reason": "authoritative", "certified_input_count": 4, **options}
    return ResolvedColumn(
        target_type=native_type,
        source_type="BOOLEAN",
        source_backend=backend,
        target_backend=backend,
        **configuration,
    )


@pytest.mark.parametrize(
    ("backend", "native_type", "cases"),
    [
        ("gp", "TEXT", _GP_TEXT),
        ("gp", "VARCHAR(32)", _GP_TEXT),
        ("gp", "character varying(32)", _GP_TEXT),
        ("trino", "VARCHAR(32)", _TRINO_TEXT),
    ],
)
def test_explicit_boolean_validation_uses_native_text_domains_without_changing_stage(
    backend, native_type, cases
):
    stage = _stage(native_type, backend, certified_input_count=len(cases))
    before = asdict(stage)
    values = [value for value, _expected in cases]
    original = list(values)

    validator = explicit_text_boolean_validation(stage, "BOOLEAN", backend)

    assert isinstance(validator, ResolvedColumn)
    assert validator is not stage
    assert validator.target_type == "BOOLEAN"
    assert validator.source_type == native_type
    assert validator.source_backend == backend
    assert validator.target_backend == backend
    assert validator.certified_input_count == len(cases)
    converted = [validator.convert(value) for value in values]
    assert all(actual is expected for actual, (_value, expected) in zip(converted, cases))
    assert [stage.convert(value) for value in values] == original
    assert values == original
    assert asdict(stage) == before


@pytest.mark.parametrize("backend", ["gp", "trino"])
@pytest.mark.parametrize(
    ("reason", "fallback"),
    [("complete value profile", False), ("no exact native representation", True)],
)
def test_explicit_boolean_validation_does_not_adopt_inferred_or_fallback_text(
    backend, reason, fallback
):
    stage = _stage("VARCHAR", backend, reason=reason, text_fallback=fallback)
    before = asdict(stage)

    assert explicit_text_boolean_validation(stage, "BOOLEAN", backend) is None

    assert asdict(stage) == before


@pytest.mark.parametrize(
    ("backend", "stage_type", "target_type"),
    [
        ("ch", "String", "Bool"),
        ("gp", "CHAR(5)", "BOOLEAN"),
        ("trino", "CHAR(5)", "BOOLEAN"),
        ("gp", "BIGINT", "BOOLEAN"),
        ("trino", "BIGINT", "BOOLEAN"),
        ("gp", "BYTEA", "BOOLEAN"),
        ("trino", "VARBINARY", "BOOLEAN"),
        ("gp", "TEXT", "DATE"),
        ("trino", "VARCHAR", "VARCHAR"),
        ("gp", "TEXT", "BOOLEAN[]"),
        ("trino", "VARCHAR", "ARRAY(BOOLEAN)"),
        ("gp", "TEXT", '"sandbox"."custom_bool"'),
    ],
)
def test_explicit_boolean_validation_leaves_other_native_contexts_unchanged(
    backend, stage_type, target_type
):
    stage = _stage(stage_type, backend)
    before = asdict(stage)

    assert explicit_text_boolean_validation(stage, target_type, backend) is None

    assert asdict(stage) == before


@pytest.mark.parametrize("backend", ["gp", "trino"])
@pytest.mark.parametrize("value", [True, 7, Decimal(7), b"false", {"private": 1}, ("true",)])
def test_explicit_boolean_validation_requires_the_actual_intermediate_text_domain(backend, value):
    validator = explicit_text_boolean_validation(_stage("VARCHAR", backend), "BOOLEAN", backend)
    assert isinstance(validator, ResolvedColumn)

    with pytest.raises(InvalidSqlInputError, match=r"CAST.*AS BOOLEAN") as failure:
        validator.convert(value)

    assert "from_sql" in str(failure.value)
    assert "private" not in str(failure.value)
    assert failure.value.__cause__ is None


@pytest.mark.parametrize(
    ("backend", "value", "error_type"),
    [
        ("gp", value, GreenplumBooleanValueError)
        for value in (
            "",
            "o",
            "truejunk",
            "tr ue",
            "fal\u017fe",
            "\u00a0true",
            "false\x00",
            "private-bool",
        )
    ]
    + [
        ("trino", value, TrinoBooleanValueError)
        for value in ("", "yes", "on", "of", "tr", "ye", " true ", "fal\u017fe", "private-bool")
    ],
)
def test_explicit_boolean_validation_preserves_private_backend_parser_failures(
    backend, value, error_type
):
    validator = explicit_text_boolean_validation(_stage("VARCHAR", backend), "BOOLEAN", backend)
    assert isinstance(validator, ResolvedColumn)

    with pytest.raises(error_type, match="BOOLEAN") as failure:
        validator.convert(value)

    message = str(failure.value)
    assert "CAST" in message
    assert "from_sql" in message
    if len(value) > 3:
        assert value not in message
    assert failure.value.__cause__ is None


@pytest.mark.parametrize(("backend", "cases"), [("gp", _GP_TEXT), ("trino", _TRINO_TEXT)])
@pytest.mark.parametrize("invalid_last", [False, True], ids=["all-valid", "late-private-invalid"])
def test_explicit_boolean_validation_checks_the_complete_spool_without_rewriting_it(
    backend, cases, invalid_last
):
    rows = [(value,) for value, _expected in cases]
    if invalid_last:
        rows.append(("private-late-boolean",))
    stage = _stage("VARCHAR", backend, certified_input_count=len(rows))
    before = asdict(stage)
    profile = ColumnProfile(native_type="VARCHAR", source_backend=backend)
    for row in rows:
        profile.add(row[0])
    validators = resolve_final_conversions(
        [SourceColumn("flag", "VARCHAR")],
        [stage],
        [profile],
        backend=backend,
        existing_types={"flag": "BOOLEAN"},
        destination_timezone=None,
    )
    assert validators is not None

    with ReplaySpool(["flag"]) as spool:
        spool.write_rows(rows)
        spool.certify()
        if invalid_last:
            with pytest.raises(ValueError, match="resolved destination schema") as failure:
                validate_final_conversions(spool, [stage], validators)
            assert "private-late-boolean" not in str(failure.value)
            expected_error = (
                GreenplumBooleanValueError if backend == "gp" else TrinoBooleanValueError
            )
            assert isinstance(failure.value.__cause__, expected_error)
            assert "private-late-boolean" not in str(failure.value.__cause__)
        else:
            validate_final_conversions(spool, [stage], validators)
        assert spool.certified
        assert spool.row_count == len(rows)
        assert list(spool.iter_rows()) == rows
        assert asdict(stage) == before
        path = spool.path
    assert not path.exists()
