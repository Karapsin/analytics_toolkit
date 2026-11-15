"""Existing DATE targets survive invalid explicit text-stage assignments."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.connection.errors import InvalidSqlInputError
from analytics_toolkit.sql.core.identifiers import TableIdentifier
from sqlglot import exp, parse_one

from tests.sql.integration._support.backends import integration_table
from tests.sql.integration._support.reconfiguration import label

if TYPE_CHECKING:
    from typing import Any

    from tests.sql.integration._support.resources import ResourceRegistry

pytestmark = [pytest.mark.integration, pytest.mark.integration_core]
_TARGET_ALIAS = "trino_target_values"


def _owned_target(registry: ResourceRegistry) -> str:
    table = integration_table("trino", "date_domain_preflight")
    acknowledged = False

    def cleanup() -> None:
        if acknowledged:
            sql.execute(
                _TARGET_ALIAS,
                f"DROP TABLE IF EXISTS {table}",
                retry_cnt=1,
                query_label=label("date-domain-cleanup"),
            )

    registry.finalizer(cleanup)
    sql.execute(
        _TARGET_ALIAS,
        f"CREATE TABLE {table} (id DATE)",
        retry_cnt=1,
        query_label=label("date-domain-create"),
    )
    acknowledged = True
    sql.execute(
        _TARGET_ALIAS,
        f"INSERT INTO {table} VALUES (DATE '1999-12-31'), (NULL)",
        retry_cnt=1,
        query_label=label("date-domain-seed"),
    )
    return table


def _rows(table: str) -> list[str | None]:
    frame = sql.read(
        _TARGET_ALIAS,
        f"SELECT CAST(id AS VARCHAR) AS id FROM {table} ORDER BY id NULLS LAST",
        retry_cnt=1,
        query_label=label("date-domain-rows"),
    )
    return [None if pd.isna(value) else str(value) for value in frame["id"]]


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


def _transfer(table: str, source: str, native: str):
    return sql.transfer(
        from_db="trino_source_values",
        to_db=_TARGET_ALIAS,
        from_sql=source,
        to_table=table,
        table_schema={"id": native},
        write_mode="truncate_insert",
        validate_row_count=True,
        return_metadata=True,
        retry_cnt=1,
        full_retry_cnt=1,
        timeout_increment=0,
        full_timeout_increment=0,
        query_label=label("date-domain-transfer"),
    )


@pytest.mark.sql_scenario("audit.transfer.trino_date_domain_preflight")
def test_invalid_text_stage_cannot_clear_existing_date_rows(
    monkeypatch: pytest.MonkeyPatch,
    resource_registry: ResourceRegistry,
) -> None:
    table = _owned_target(resource_registry)
    original_schema = sql.extract_ddl(_TARGET_ALIAS, table)
    original_rows = ["1999-12-31", None]
    assert _rows(table) == original_rows
    adapter = get_backend_adapter("trino")
    execute = adapter.execute_command
    statements: list[str] = []

    def record(connection: Any, statement: str) -> Any:
        statements.append(statement)
        return execute(connection, statement)

    malformed = (
        "SELECT CAST('2024-01-01' AS VARCHAR) AS id "
        "UNION ALL SELECT CAST(NULL AS VARCHAR) AS id "
        "UNION ALL SELECT CAST('not-a-date' AS VARCHAR) AS id"
    )
    with monkeypatch.context() as operations:
        operations.setattr(adapter, "execute_command", record)
        with pytest.raises(InvalidSqlInputError):
            _transfer(table, malformed, "VARCHAR")
    assert _target_mutations(statements, table) == []
    assert _rows(table) == original_rows
    assert sql.extract_ddl(_TARGET_ALIAS, table) == original_schema

    controls = [
        (
            "SELECT CAST('2024-02-29' AS VARCHAR) AS id "
            "UNION ALL SELECT CAST(NULL AS VARCHAR) AS id",
            "VARCHAR",
        ),
        (
            "SELECT DATE '2024-02-29' AS id UNION ALL SELECT CAST(NULL AS DATE) AS id",
            "DATE",
        ),
    ]
    for source, native in controls:
        result = _transfer(table, source, native)
        assert result.rows == 2
        assert result.metadata.row_count_validated is True
        assert _rows(table) == ["2024-02-29", None]
        assert sql.extract_ddl(_TARGET_ALIAS, table) == original_schema
