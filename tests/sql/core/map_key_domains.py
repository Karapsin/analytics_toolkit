"""Metadata streaming needs injective key coercions, not only fitting domains."""

import pytest
from analytics_toolkit.sql.backends.map_key_domains import (
    _key_family,
    map_key_conversion_is_injective,
)
from analytics_toolkit.sql.backends.type_resolution import declared_domain_fits


@pytest.mark.parametrize(
    ("source", "target", "injective"),
    [
        ("VARCHAR(2)", "CHAR(2)", False),
        ("VARCHAR(2)", "CHAR(4)", False),
        ("VARCHAR(2)", "VARCHAR(2)", True),
        ("VARCHAR(2)", "VARCHAR(4)", True),
        ("CHAR(2)", "CHAR(4)", True),
        ("CHAR(2)", "VARCHAR(4)", True),
        ("SMALLINT", "BIGINT", True),
        ("TINYINT", "DECIMAL(4,0)", True),
        ("DECIMAL(4,1)", "DECIMAL(6,2)", True),
        ("REAL", "DOUBLE", True),
        ("DOUBLE", "REAL", False),
        ("date", "DATE", True),
        ("uuid", "UUID", True),
        ("boolean", "BOOLEAN", True),
        ("VARBINARY(2)", "VARBINARY(4)", True),
        ("VARBINARY(2)", "BINARY(4)", False),
        ("TIMESTAMP(3)", "TIMESTAMP(6)", True),
        ("ARRAY(VARCHAR(2))", "ARRAY(CHAR(2))", False),
        ("ARRAY(VARCHAR(2))", "ARRAY(VARCHAR(4))", True),
        ('ROW("Key" VARCHAR(2), n BIGINT)', 'ROW("Key" CHAR(2), n BIGINT)', False),
        ('ROW("Key" VARCHAR(2), n INTEGER)', 'ROW("Key" VARCHAR(4), n BIGINT)', True),
        ('ARRAY(ROW("Key" VARCHAR(2)))', 'ARRAY(ROW("Key" CHAR(2)))', False),
    ],
)
def test_key_conversion_proofs_preserve_native_identity(source, target, injective):
    assert map_key_conversion_is_injective(source, target, "trino", "trino") is injective


def test_map_only_restriction_recurses_without_changing_scalar_char_conversion():
    assert declared_domain_fits("VARCHAR(2)", "CHAR(2)", "trino", source_backend="trino")
    for wrapper in ["{}", "ARRAY({})", 'ROW("Payload" {})']:
        source = wrapper.format("MAP(VARCHAR(2), VARCHAR)")
        target = wrapper.format("MAP(CHAR(2), VARCHAR)")
        assert not declared_domain_fits(source, target, "trino", source_backend="trino")
    assert not declared_domain_fits(
        "MAP(BIGINT, ARRAY(MAP(VARCHAR(2), VARCHAR)))",
        "MAP(BIGINT, ARRAY(MAP(CHAR(2), VARCHAR)))",
        "trino",
        source_backend="trino",
    )
    assert declared_domain_fits(
        "MAP(VARCHAR(2), INTEGER)", "MAP(VARCHAR(4), BIGINT)", "trino", source_backend="trino"
    )


def test_unproven_key_conversion_never_borrows_value_domain_membership():
    assert not map_key_conversion_is_injective("opaque", "OTHER", "trino", "trino")
    assert map_key_conversion_is_injective("opaque", "opaque", "trino", "trino")
    assert not map_key_conversion_is_injective(
        "ARRAY(VARCHAR(2))", "ROW(x VARCHAR(2))", "trino", "trino"
    )


def test_unknown_and_noncomparable_key_structures_are_not_assumed_injective():
    assert _key_family("opaque", "trino") is None
    assert not map_key_conversion_is_injective(
        "MAP(VARCHAR(2), INTEGER)", "MAP(VARCHAR(4), BIGINT)", "trino", "trino"
    )
    assert map_key_conversion_is_injective("Tuple(Int8)", "ROW(x SMALLINT)", "trino", "ch")
