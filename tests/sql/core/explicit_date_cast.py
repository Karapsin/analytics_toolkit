"""Explicit DATE-cast validation stays separate from inference and payloads."""

from __future__ import annotations

import datetime as dt
from dataclasses import asdict

import pytest
from analytics_toolkit.sql.backends.explicit_date_cast import explicit_text_date_validation
from analytics_toolkit.sql.backends.type_resolution import ResolvedColumn
from analytics_toolkit.sql.connection.errors import InvalidSqlInputError


def _stage(native_type, backend, **options):
    return ResolvedColumn(
        target_type=native_type,
        source_type="DATE",
        source_backend=backend,
        target_backend=backend,
        certified_input_count=4,
        **{"reason": "authoritative", **options},
    )


@pytest.mark.parametrize(
    ("backend", "native_type"),
    [("gp", "TEXT"), ("gp", "VARCHAR(10)"), ("trino", "VARCHAR(10)")],
)
def test_explicit_date_validator_retains_iso_calendar_values_null_and_stage_payload(
    backend, native_type
):
    stage = _stage(native_type, backend)
    before = asdict(stage)
    values = ["0001-01-01", "2024-02-29", "9999-12-31", None]
    original = list(values)

    validator = explicit_text_date_validation(stage, "DATE", backend)

    assert isinstance(validator, ResolvedColumn)
    assert validator is not stage
    assert validator.target_type == "DATE"
    assert validator.source_type == native_type
    assert validator.target_backend == backend
    assert [validator.convert(value) for value in values] == [
        dt.date(1, 1, 1),
        dt.date(2024, 2, 29),
        dt.date(9999, 12, 31),
        None,
    ]
    assert [stage.convert(value) for value in values] == original
    assert values == original
    assert asdict(stage) == before


@pytest.mark.parametrize(
    ("backend", "native_type", "reason", "fallback"),
    [
        ("gp", "TEXT", "complete value profile", False),
        ("trino", "VARCHAR", "no exact native representation", True),
    ],
)
def test_explicit_date_validator_does_not_adopt_inferred_or_fallback_text(
    backend, native_type, reason, fallback
):
    stage = _stage(native_type, backend, reason=reason, text_fallback=fallback)
    before = asdict(stage)

    assert explicit_text_date_validation(stage, "DATE", backend) is None

    assert asdict(stage) == before


@pytest.mark.parametrize(
    ("backend", "native_type", "target_type"),
    [
        ("gp", "BIGINT", "DATE"),
        ("gp", "VARCHAR", "TIMESTAMP(6)"),
        ("trino", "VARBINARY", "DATE"),
        ("ch", "String", "Date"),
    ],
)
def test_explicit_date_validator_leaves_other_supported_native_contexts_unchanged(
    backend, native_type, target_type
):
    stage = _stage(native_type, backend)

    assert explicit_text_date_validation(stage, target_type, backend) is None


@pytest.mark.parametrize("backend", ["gp", "trino"])
@pytest.mark.parametrize("value", [86400, True, dt.date(2024, 2, 29), b"2024-02-29"])
def test_explicit_date_validator_rejects_nontext_values_without_epoch_semantics(backend, value):
    stage = _stage("VARCHAR", backend)
    validator = explicit_text_date_validation(stage, "DATE", backend)
    assert isinstance(validator, ResolvedColumn)

    with pytest.raises(InvalidSqlInputError, match="CAST the source expression AS DATE") as error:
        validator.convert(value)

    assert str(value) not in str(error.value)
    assert error.value.__cause__ is None


@pytest.mark.parametrize("backend", ["gp", "trino"])
@pytest.mark.parametrize(
    "value",
    [
        "2023-02-29",
        "0000-01-01",
        "2024-2-29",
        "private-stage-date",
        "\uff12\uff10\uff12\uff14-\uff10\uff12-\uff12\uff19",
    ],
)
def test_explicit_date_validator_rejects_noncanonical_or_invalid_calendar_text_privately(
    backend, value
):
    validator = explicit_text_date_validation(_stage("VARCHAR", backend), "DATE", backend)
    assert isinstance(validator, ResolvedColumn)

    with pytest.raises(InvalidSqlInputError, match="canonical YYYY-MM-DD") as error:
        validator.convert(value)

    assert "from_sql" in str(error.value)
    assert value not in str(error.value)
    assert error.value.__cause__ is None
