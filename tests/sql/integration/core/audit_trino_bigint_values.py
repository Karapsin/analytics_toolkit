"""Authoritative Trino BIGINT loads preserve historical integer conversion."""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.core.identifiers import TableIdentifier
from sqlglot import exp, parse_one

from tests.sql.integration._support.backends import integration_table
from tests.sql.integration._support.reconfiguration import label

if TYPE_CHECKING:
    from typing import Any

    from tests.sql.integration._support.resources import ResourceRegistry

pytestmark = [pytest.mark.integration, pytest.mark.integration_core]
_ALIAS = "trino_target_values"
_LOWER = -(2**63)
_UPPER = 2**63 - 1


def _owned_table(registry: ResourceRegistry) -> str:
    table = integration_table("trino", "bigint_native_values")
    acknowledged = False

    def cleanup() -> None:
        if acknowledged:
            sql.execute(
                _ALIAS,
                f"DROP TABLE IF EXISTS {table}",
                retry_cnt=1,
                query_label=label("bigint-native-cleanup"),
            )

    registry.finalizer(cleanup)
    sql.execute(
        _ALIAS,
        f"CREATE TABLE {table} (payload BIGINT)",
        retry_cnt=1,
        query_label=label("bigint-native-create"),
    )
    acknowledged = True
    return table


def _rows(table: str) -> list[int | None]:
    frame = sql.read(
        _ALIAS,
        f"SELECT payload FROM {table} ORDER BY payload NULLS LAST",
        retry_cnt=1,
        query_label=label("bigint-native-rows"),
    )
    return [None if pd.isna(value) else int(value) for value in frame["payload"]]


def _target_mutations(statements: list[str], table: str) -> list[str]:
    expected = TableIdentifier.parse(table, "trino").parts
    mutations = []
    for statement in statements:
        tree = parse_one(statement, read="trino")
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


@pytest.mark.sql_scenario("audit.load.trino_bigint_values")
def test_trino_bigint_loads_preserve_integer_strings_and_reject_invalid_last_values(
    monkeypatch: pytest.MonkeyPatch,
    resource_registry: ResourceRegistry,
) -> None:
    table = _owned_table(resource_registry)
    original_schema = sql.extract_ddl(_ALIAS, table)
    incoming = pd.DataFrame(
        {
            "payload": pd.Series(
                [
                    None,
                    "0",
                    "+0007",
                    " \t-2\n",
                    "42",
                    1.9,
                    -1.9,
                    Decimal(f"{_LOWER}.9"),
                    Decimal(f"{_UPPER}.9"),
                    str(_LOWER),
                    str(_UPPER),
                    _LOWER,
                    _UPPER,
                    None,
                ],
                dtype=object,
            )
        }
    )
    original = incoming.copy(deep=True)
    original_types = [type(value) for value in incoming["payload"]]
    expected = [
        _LOWER,
        _LOWER,
        _LOWER,
        -2,
        -1,
        0,
        1,
        7,
        42,
        _UPPER,
        _UPPER,
        _UPPER,
        None,
        None,
    ]
    adapter = get_backend_adapter("trino")
    execute = adapter.execute_command
    statements: list[str] = []

    def record(connection: Any, statement: str) -> Any:
        statements.append(statement)
        return execute(connection, statement)

    # The values alias exercises the real SDK BIGINT parameter casts.
    for explicit in (True, False):
        options = {"table_schema": {"payload": "BIGINT"}} if explicit else {}
        assert sql.load_df(
            _ALIAS,
            table,
            incoming,
            write_mode="truncate_insert",
            trino_insert_chunk_size=2,
            retry_cnt=1,
            timeout_increment=0,
            query_label=label("bigint-native-load"),
            **options,
        ) == len(incoming)
        pd.testing.assert_frame_equal(incoming, original)
        assert [type(value) for value in incoming["payload"]] == original_types
        assert _rows(table) == expected
        assert sql.extract_ddl(_ALIAS, table) == original_schema

        for bad in (
            "private-invalid-bigint",
            str(_LOWER - 1),
            str(_UPPER + 1),
            _LOWER - 1,
            _UPPER + 1,
        ):
            invalid = incoming.copy(deep=True)
            invalid.loc[len(invalid) - 1, "payload"] = bad
            original_invalid = invalid.copy(deep=True)
            statements.clear()
            with monkeypatch.context() as operations:
                operations.setattr(adapter, "execute_command", record)
                with pytest.raises(ValueError, match=r"(?i)bigint|integer|range") as error:
                    sql.load_df(
                        _ALIAS,
                        table,
                        invalid,
                        write_mode="truncate_insert",
                        trino_insert_chunk_size=2,
                        retry_cnt=1,
                        timeout_increment=0,
                        query_label=label("bigint-native-invalid"),
                        **options,
                    )
            assert "private-invalid-bigint" not in str(error.value)
            pd.testing.assert_frame_equal(invalid, original_invalid)
            assert _target_mutations(statements, table) == []
            assert _rows(table) == expected
            assert sql.extract_ddl(_ALIAS, table) == original_schema
