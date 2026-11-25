"""Recognized declarations are validated even when planning has no value rows."""

from __future__ import annotations

import pytest
from analytics_toolkit import sql


@pytest.mark.parametrize(
    ("backend", "native"),
    [
        ("gp", "VARCHAR(1, 2)"),
        ("gp", "TIMESTAMP(6, 7)"),
        ("gp", "NUMERIC(\uff13\uff18, 0)"),
        ("trino", "VARCHAR(1, 2)"),
        ("trino", "ARRAY()"),
        ("trino", "DECIMAL(\uff13\uff18, 0)"),
        ("ch", "String(1)"),
        ("ch", "Array()"),
        ("ch", "Decimal(\uff13\uff18, 0)"),
    ],
)
def test_public_sql_only_create_rejects_invalid_known_type_declaration(backend, native):
    with pytest.raises(ValueError, match=r"(?i)type|declaration|arity|precision|scale|argument"):
        sql.create_table(
            backend,
            "sandbox.events",
            table_schema={"payload": native},
            only_generate_sql=True,
            ch_distributed_table=False,
        )


@pytest.mark.parametrize(
    ("backend", "native"),
    [("gp", "VARCHAR(2)"), ("trino", "ARRAY(TIMESTAMP(9))"), ("ch", "Array(Nullable(String))")],
)
def test_public_sql_only_create_keeps_valid_declared_nested_and_scalar_types(backend, native):
    ddl = sql.create_table(
        backend,
        "sandbox.events",
        table_schema={"payload": native},
        only_generate_sql=True,
        ch_distributed_table=False,
    )
    assert native in ddl
