"""Finite typed numeric text preserves native values, exact decimals and zero bits."""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.core.identifiers import TableIdentifier
from sqlglot import exp, parse_one

from tests.sql.integration._support.backends import (
    backend_alias,
    backend_enabled,
    integration_table,
)
from tests.sql.integration._support.reconfiguration import label
from tests.sql.integration.manifest import scenario_param

if TYPE_CHECKING:
    from typing import Any

    from tests.sql.integration._support.resources import ResourceRegistry

pytestmark = [pytest.mark.integration, pytest.mark.integration_core]
_EXACT = "12345678901234567890.123456789"


def _schema(backend: str) -> dict[str, str]:
    return {
        "gp": {
            "row_id": "BIGINT",
            "small_float": "REAL",
            "wide_float": "DOUBLE PRECISION",
            "amount": "NUMERIC(38,9)",
        },
        "trino": {
            "row_id": "BIGINT",
            "small_float": "REAL",
            "wide_float": "DOUBLE",
            "amount": "DECIMAL(38,9)",
        },
        "ch": {
            "row_id": "Int64",
            "small_float": "Nullable(Float32)",
            "wide_float": "Nullable(Float64)",
            "amount": "Nullable(Decimal(38,9))",
        },
    }[backend]


def _owned_table(registry: ResourceRegistry, backend: str, alias: str) -> str:
    table = integration_table(backend, "finite_numeric_text")
    acknowledged = False

    def cleanup() -> None:
        if acknowledged:
            sql.execute(
                alias,
                f"DROP TABLE IF EXISTS {table}",
                retry_cnt=1,
                query_label=label("numeric-text-cleanup"),
            )

    registry.finalizer(cleanup)
    adapter = get_backend_adapter(backend)
    columns = ", ".join(
        f"{adapter.quote_identifier(name)} {native}" for name, native in _schema(backend).items()
    )
    suffix = {
        "gp": " DISTRIBUTED BY (row_id)",
        "trino": "",
        "ch": " ENGINE=MergeTree ORDER BY row_id",
    }[backend]
    sql.execute(
        alias,
        f"CREATE TABLE {table} ({columns}){suffix}",
        retry_cnt=1,
        query_label=label("numeric-text-create"),
    )
    acknowledged = True
    return table


def _frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "row_id": range(1, 8),
            "small_float": pd.Series(
                ["1.25", "-2.5", 0.0, -0.0, "0.0", "-0.0", None], dtype=object
            ),
            "wide_float": pd.Series(["-2.5", "1.25", 0.0, -0.0, "0.0", "-0.0", None], dtype=object),
            "amount": pd.Series(
                [
                    _EXACT,
                    f"-{_EXACT}",
                    Decimal("0.000000000"),
                    "-0.000000000",
                    "1.250000000",
                    Decimal("-2.500000000"),
                    None,
                ],
                dtype=object,
            ),
        }
    )


def _rows(alias: str, table: str, backend: str) -> list[tuple[Any, ...]]:
    decimal_text = "toString(amount)" if backend == "ch" else "CAST(amount AS VARCHAR)"
    frame = sql.read(
        alias,
        f"SELECT row_id, small_float, wide_float, {decimal_text} AS exact_amount "
        f"FROM {table} ORDER BY row_id",
        retry_cnt=1,
        query_label=label("numeric-text-rows"),
    )
    return [
        (
            int(row_id),
            None if pd.isna(small) else float(small),
            None if pd.isna(wide) else float(wide),
            None if pd.isna(amount) else Decimal(str(amount)),
        )
        for row_id, small, wide, amount in frame.itertuples(index=False, name=None)
    ]


def _zero_bits(alias: str, table: str, backend: str) -> list[tuple[int, int | None, int | None]]:
    functions = {
        "gp": (
            "encode(float4send(small_float), 'hex')",
            "encode(float8send(wide_float), 'hex')",
        ),
        "trino": (
            "to_hex(to_ieee754_32(small_float))",
            "to_hex(to_ieee754_64(wide_float))",
        ),
        "ch": (
            "toString(reinterpretAsUInt32(small_float))",
            "toString(reinterpretAsUInt64(wide_float))",
        ),
    }
    small, wide = functions[backend]
    frame = sql.read(
        alias,
        f"SELECT row_id, {small} AS small_bits, {wide} AS wide_bits "
        f"FROM {table} WHERE row_id IN (3, 4, 5, 6, 7) ORDER BY row_id",
        retry_cnt=1,
        query_label=label("numeric-text-zero-bits"),
    )
    radix = 10 if backend == "ch" else 16
    return [
        (
            int(row_id),
            None if pd.isna(left) else int(str(left), radix),
            None if pd.isna(right) else int(str(right), radix),
        )
        for row_id, left, right in frame.itertuples(index=False, name=None)
    ]


def _target_mutations(statements: list[str], table: str, backend: str) -> list[str]:
    expected = TableIdentifier.parse(table, backend).parts
    dialect = get_backend_adapter(backend).sqlglot_dialect
    mutations = []
    for statement in statements:
        tree = parse_one(statement, read=dialect)
        if isinstance(
            tree,
            (
                exp.Delete,
                exp.Insert,
                exp.Update,
                exp.Drop,
                exp.Alter,
                exp.Create,
                exp.TruncateTable,
            ),
        ) and any(
            tuple(part.name for part in item.parts) == expected for item in tree.find_all(exp.Table)
        ):
            mutations.append(statement)
    return mutations


@pytest.mark.parametrize(
    "backend",
    [
        scenario_param("audit.load.gp_finite_numeric_text", "gp"),
        scenario_param("audit.load.trino_finite_numeric_text", "trino"),
        scenario_param("audit.load.clickhouse_finite_numeric_text", "ch"),
    ],
)
def test_finite_numeric_text_loads_preserve_exact_values_and_reject_invalid_last_rows(
    monkeypatch: pytest.MonkeyPatch,
    resource_registry: ResourceRegistry,
    backend: str,
) -> None:
    if not backend_enabled(backend):
        pytest.skip("Greenplum requires x86_64")
    alias = backend_alias(backend, target=True)
    table = _owned_table(resource_registry, backend, alias)
    original_schema = sql.extract_ddl(alias, table)
    incoming = _frame()
    original = incoming.copy(deep=True)
    expected = [
        (1, 1.25, -2.5, Decimal(_EXACT)),
        (2, -2.5, 1.25, Decimal(f"-{_EXACT}")),
        (3, 0.0, 0.0, Decimal(0)),
        (4, -0.0, -0.0, Decimal(0)),
        (5, 0.0, 0.0, Decimal("1.25")),
        (6, -0.0, -0.0, Decimal("-2.5")),
        (7, None, None, None),
    ]
    expected_bits = [(3, 0, 0), (4, 2**31, 2**63), (5, 0, 0), (6, 2**31, 2**63), (7, None, None)]
    adapter = get_backend_adapter(backend)
    execute = adapter.execute_command
    statements: list[str] = []

    def record(connection: Any, statement: str) -> Any:
        statements.append(statement)
        return execute(connection, statement)

    # The CH native case uses the candidate's typed conversion, not a claim
    # that the legacy native driver accepted raw Float strings directly.
    base = {"ch_distributed_table": False} if backend == "ch" else {}
    for explicit in (True, False):
        options = {**base, **({"table_schema": _schema(backend)} if explicit else {})}
        assert sql.load_df(
            alias,
            table,
            incoming,
            write_mode="truncate_insert",
            retry_cnt=1,
            timeout_increment=0,
            query_label=label("numeric-text-load"),
            **options,
        ) == len(incoming)
        pd.testing.assert_frame_equal(incoming, original)
        assert _rows(alias, table, backend) == expected
        assert _zero_bits(alias, table, backend) == expected_bits
        assert sql.extract_ddl(alias, table) == original_schema

        invalid_cases = [
            ("small_float", "private-invalid-numeric"),
            ("small_float", "3.5e38"),
            ("wide_float", "1e309"),
            ("wide_float", "NaN"),
            ("wide_float", "Infinity"),
            ("amount", "private-invalid-numeric"),
            ("amount", f"{10**29}.000000000"),
            ("amount", "1.0000000001"),
            ("amount", "NaN"),
        ]
        for column, bad in invalid_cases:
            invalid = incoming.copy(deep=True)
            invalid.loc[len(invalid) - 1, column] = bad
            original_invalid = invalid.copy(deep=True)
            statements.clear()
            with monkeypatch.context() as operations:
                operations.setattr(adapter, "execute_command", record)
                with pytest.raises(
                    ValueError, match=r"(?i)numeric|decimal|float|range|finite"
                ) as error:
                    sql.load_df(
                        alias,
                        table,
                        invalid,
                        write_mode="truncate_insert",
                        retry_cnt=1,
                        timeout_increment=0,
                        query_label=label("numeric-text-invalid"),
                        **options,
                    )
            assert "private-invalid-numeric" not in str(error.value)
            pd.testing.assert_frame_equal(invalid, original_invalid)
            assert _target_mutations(statements, table, backend) == []
            assert _rows(alias, table, backend) == expected
            assert _zero_bits(alias, table, backend) == expected_bits
            assert sql.extract_ddl(alias, table) == original_schema
