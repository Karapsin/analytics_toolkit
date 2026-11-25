from decimal import Decimal

import pytest
from analytics_toolkit.sql.backends.conversion_profiles import validate_converted_rows
from analytics_toolkit.sql.backends.type_resolution import ResolvedColumn
from analytics_toolkit.sql.core.exact_values import ExactTemporal


@pytest.mark.parametrize("backend", ["gp", "trino"])
def test_converted_profile_measures_complete_structured_text(backend: str) -> None:
    resolution = ResolvedColumn(
        "VARCHAR(65)",
        source_type="Array(Int64)",
        source_backend="ch",
        target_backend=backend,
    )
    expected = '{"v":1,"backend":"ch","type":"Array(Int64)","value":["array",[]]}'
    assert resolution.convert([]) == expected
    profiles = validate_converted_rows([(None,), ([],)], [resolution], backend=backend)
    assert profiles[0].maximum_text_length == len(expected) == 65
    assert profiles[0].count == 2
    assert profiles[0].null_count == 1
    with pytest.raises(ValueError, match="authoritative text length"):
        validate_converted_rows(
            [(None,), ([],)],
            [
                ResolvedColumn(
                    "VARCHAR(60)",
                    source_type="Array(Int64)",
                    source_backend="ch",
                    target_backend=backend,
                ),
            ],
            backend=backend,
        )


@pytest.mark.parametrize("target_type", ["VARCHAR(2)", "CHAR(2)", "CHARACTER VARYING(2)"])
def test_converted_text_bounds_count_characters(target_type: str) -> None:
    profiles = validate_converted_rows(
        [("é😀",)],
        [ResolvedColumn(target_type, target_backend="gp")],
        backend="gp",
    )
    assert profiles[0].maximum_text_length == 2
    assert profiles[0].maximum_byte_length == 6


@pytest.mark.parametrize(
    ("value", "target_type", "backend", "message"),
    [
        (2**63, "BIGINT", "gp", "authoritative integer"),
        (Decimal("0.001"), "DECIMAL(4, 2)", "trino", "authoritative decimal"),
        ("éé", "FixedString(3)", "ch", "FixedString"),
        (None, "String", "ch", "nonnullable"),
        ("a\x00b", "TEXT", "gp", "Greenplum text"),
    ],
)
def test_converted_values_keep_native_schema_constraints(
    value: object,
    target_type: str,
    backend: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        validate_converted_rows(
            [(value,)],
            [ResolvedColumn(target_type, target_backend=backend)],
            backend=backend,
        )


def test_converted_profile_accepts_intentional_timestamp_precision() -> None:
    profiles = validate_converted_rows(
        [(ExactTemporal(123456789, 9),)],
        [ResolvedColumn("TIMESTAMP(3)", source_type="DateTime64(9)", target_backend="trino")],
        backend="trino",
    )
    assert profiles[0].temporal_scale == 3
    assert profiles[0].minimum_timestamp_ns == 123000000


def test_converted_profile_retains_native_bytes_and_sql_null() -> None:
    resolutions = [
        ResolvedColumn("String", source_type="bytea", source_backend="gp", target_backend="ch"),
        ResolvedColumn("Nullable(String)", target_backend="ch"),
    ]
    profiles = validate_converted_rows([(b"\xff\x00", None)], resolutions, backend="ch")
    assert profiles[0].families == {"binary"}
    assert profiles[0].maximum_byte_length == 2
    assert profiles[1].null_count == profiles[1].count == 1
    assert profiles[1].maximum_text_length == 0


def test_converted_profile_rejects_incomplete_positional_rows() -> None:
    with pytest.raises(ValueError, match="does not match the resolved schema"):
        validate_converted_rows(
            [()],
            [ResolvedColumn("BIGINT", target_backend="gp")],
            backend="gp",
        )
