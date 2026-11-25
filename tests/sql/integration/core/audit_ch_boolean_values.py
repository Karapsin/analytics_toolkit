"""Authoritative ClickHouse Bool loads preserve native values on both transports."""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from analytics_toolkit.sql.core.identifiers import TableIdentifier
from sqlglot import exp, parse_one

from tests.sql.integration._support.backends import integration_table
from tests.sql.integration._support.reconfiguration import label

if TYPE_CHECKING:
    from typing import Any

    from tests.sql.integration._support.resources import ResourceRegistry

pytestmark = [pytest.mark.integration, pytest.mark.integration_core]
_ALIAS = "ch_target"


def _owned_table(registry: ResourceRegistry) -> str:
    table = integration_table("ch", "boolean_native_values")
    acknowledged = False

    def cleanup() -> None:
        if acknowledged:
            sql.execute(
                _ALIAS,
                f"DROP TABLE IF EXISTS {table}",
                retry_cnt=1,
                query_label=label("ch-boolean-cleanup"),
            )

    registry.finalizer(cleanup)
    sql.execute(
        _ALIAS,
        f"CREATE TABLE {table} (payload Nullable(Bool)) ENGINE=MergeTree ORDER BY tuple()",
        retry_cnt=1,
        query_label=label("ch-boolean-create"),
    )
    acknowledged = True
    return table


def _rows(table: str) -> list[int | None]:
    frame = sql.read(
        _ALIAS,
        f"SELECT toUInt8(payload) AS stored FROM {table} ORDER BY payload NULLS LAST",
        retry_cnt=1,
        query_label=label("ch-boolean-rows"),
    )
    return [None if pd.isna(value) else int(value) for value in frame["stored"]]


def _target_mutations(statements: list[str], table: str) -> list[str]:
    expected = TableIdentifier.parse(table, "ch").parts
    mutations = []
    for statement in statements:
        tree = parse_one(statement, read=ClickHouseDialect)
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


@pytest.mark.sql_scenario("audit.load.clickhouse_boolean_values")
def test_clickhouse_boolean_integer_loads_preserve_native_values_and_reject_invalid_input(
    monkeypatch: pytest.MonkeyPatch,
    resource_registry: ResourceRegistry,
) -> None:
    # The integration runner selects the real HTTP/native transport for this alias.
    table = _owned_table(resource_registry)
    original_schema = sql.extract_ddl(_ALIAS, table)
    incoming = pd.DataFrame(
        {
            "payload": pd.Series(
                [
                    0,
                    1,
                    False,
                    True,
                    -2,
                    2,
                    -0.0,
                    0.5,
                    -0.5,
                    Decimal(0),
                    Decimal("-0.000"),
                    Decimal("1E-1000"),
                    None,
                ],
                dtype=object,
            )
        }
    )
    original = incoming.copy(deep=True)
    original_types = [type(value) for value in incoming["payload"]]
    expected = [0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, None]
    adapter = get_backend_adapter("ch")
    execute = adapter.execute_command
    statements: list[str] = []

    def record(connection: Any, statement: str) -> Any:
        statements.append(statement)
        return execute(connection, statement)

    for explicit in (True, False):
        options = {"table_schema": {"payload": "Nullable(Bool)"}} if explicit else {}
        assert sql.load_df(
            _ALIAS,
            table,
            incoming,
            write_mode="truncate_insert",
            ch_distributed_table=False,
            retry_cnt=1,
            timeout_increment=0,
            query_label=label("ch-boolean-load"),
            **options,
        ) == len(incoming)
        pd.testing.assert_frame_equal(incoming, original)
        assert [type(value) for value in incoming["payload"]] == original_types
        assert _rows(table) == expected
        assert sql.extract_ddl(_ALIAS, table) == original_schema

        invalid = incoming.copy(deep=True)
        invalid.loc[len(invalid) - 1, "payload"] = "private-invalid-boolean"
        original_invalid = invalid.copy(deep=True)
        statements.clear()
        with monkeypatch.context() as operations:
            operations.setattr(adapter, "execute_command", record)
            with pytest.raises(ValueError, match=r"(?i)bool") as error:
                sql.load_df(
                    _ALIAS,
                    table,
                    invalid,
                    write_mode="truncate_insert",
                    ch_distributed_table=False,
                    retry_cnt=1,
                    timeout_increment=0,
                    query_label=label("ch-boolean-invalid"),
                    **options,
                )
        assert "private-invalid-boolean" not in str(error.value)
        pd.testing.assert_frame_equal(invalid, original_invalid)
        assert _target_mutations(statements, table) == []
        assert _rows(table) == expected
        assert sql.extract_ddl(_ALIAS, table) == original_schema
