"""Backend declaration checks retain supported Trino boundary spellings."""

from __future__ import annotations

import pytest
from analytics_toolkit.sql.backends.trino.date_values import TrinoDateValueError
from analytics_toolkit.sql.backends.trino.type_declarations import (
    TrinoTypeDeclarationError,
    validate_type_declaration,
)


@pytest.mark.parametrize(
    "native",
    [
        "VARCHAR",
        "VARCHAR(0)",
        "VARCHAR(2147483646)",
        "VARCHAR(2147483647)",
        "CHAR",
        "CHAR(0)",
        "CHAR(65536)",
        "DECIMAL",
        "DECIMAL(38)",
        "DECIMAL(38,38)",
        "TIMESTAMP( 9 ) WITH TIME ZONE",
        "TIMESTAMP(0)",
        "TIMESTAMP(12)",
        "TIME(0)",
        "TIME(12) WITH TIME ZONE",
        "ARRAY(JSON)",
        'ROW("spaced field" ARRAY(MAP(VARCHAR, TIMESTAMP(12))), BIGINT)',
        "INTERVAL DAY TO SECOND",
        "INTERVAL YEAR TO MONTH",
        "DOUBLE PRECISION",
    ],
)
def test_supported_trino_declarations_include_native_boundary_values(native):
    validate_type_declaration(native)


@pytest.mark.parametrize(
    "native",
    [
        "VARCHAR(2147483648)",
        "CHAR(65537)",
        "VARCHAR(1,2)",
        "VARCHAR()",
        "DECIMAL(\uff13\uff18,0)",
        "DECIMAL(0)",
        "DECIMAL(38,39)",
        "TIMESTAMP(13)",
        "TIMESTAMP(9,1)",
        "ARRAY()",
        "ARRAY(BIGINT, VARCHAR)",
        "MAP(VARCHAR)",
        "ARRAY(opaque_type)",
        "ROW(a TIME(13), b BIGINT)",
    ],
)
def test_invalid_trino_declarations_cannot_hide_behind_null_cells(native):
    with pytest.raises(TrinoTypeDeclarationError):
        validate_type_declaration(native)


def test_parameterized_date_declaration_keeps_its_native_error_contract():
    with pytest.raises(TrinoDateValueError, match="unparameterized DATE type"):
        validate_type_declaration("DATE(1)")
