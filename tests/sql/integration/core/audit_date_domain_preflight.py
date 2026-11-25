"""Existing DATE targets survive invalid explicit text-stage assignments."""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING

import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.connection.errors import InvalidSqlInputError
from analytics_toolkit.sql.core.identifiers import TableIdentifier
from sqlglot import exp, parse_one

from tests.sql.integration._support.backends import backend_enabled, integration_table
from tests.sql.integration._support.reconfiguration import label

if TYPE_CHECKING:
    from typing import Any

    from tests.sql.integration._support.resources import ResourceRegistry

pytestmark = [pytest.mark.integration, pytest.mark.integration_core]
_TARGET_ALIAS = "trino_target_values"


def _owned_target(
    registry: ResourceRegistry,
    *,
    purpose: str = "date_domain_preflight",
    backend: str = "trino",
    alias: str = _TARGET_ALIAS,
) -> str:
    table = integration_table(backend, purpose)
    acknowledged = False

    def cleanup() -> None:
        if acknowledged:
            sql.execute(
                alias,
                f"DROP TABLE IF EXISTS {table}",
                retry_cnt=1,
                query_label=label("date-domain-cleanup"),
            )

    registry.finalizer(cleanup)
    distribution = " DISTRIBUTED RANDOMLY" if backend == "gp" else ""
    sql.execute(
        alias,
        f"CREATE TABLE {table} (id DATE){distribution}",
        retry_cnt=1,
        query_label=label("date-domain-create"),
    )
    acknowledged = True
    sql.execute(
        alias,
        f"INSERT INTO {table} VALUES (DATE '1999-12-31'), (NULL)",
        retry_cnt=1,
        query_label=label("date-domain-seed"),
    )
    return table


def _rows(table: str, *, alias: str = _TARGET_ALIAS) -> list[str | None]:
    frame = sql.read(
        alias,
        f"SELECT CAST(id AS VARCHAR) AS id FROM {table} ORDER BY id NULLS LAST",
        retry_cnt=1,
        query_label=label("date-domain-rows"),
    )
    return [None if pd.isna(value) else str(value) for value in frame["id"]]


def _target_mutations(statements: list[str], table: str, *, backend: str = "trino") -> list[str]:
    expected = TableIdentifier.parse(table, backend).parts
    mutations = []
    for statement in statements:
        tree = parse_one(statement, read="postgres" if backend == "gp" else "trino")
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


@pytest.mark.sql_scenario("audit.load.trino_date_values")
def test_trino_date_dataframe_loads_preserve_local_calendar_and_reject_invalid_values(
    monkeypatch: pytest.MonkeyPatch,
    resource_registry: ResourceRegistry,
) -> None:
    table = _owned_target(resource_registry, purpose="date_dataframe_preflight")
    original_schema = sql.extract_ddl(_TARGET_ALIAS, table)
    incoming = pd.DataFrame(
        {
            "id": pd.Series(
                [
                    None,
                    "2024-02-29",
                    pd.Timestamp("2024-02-29 12:34:56"),
                    pd.Timestamp("2024-03-01T00:30:00+14:00"),
                    dt.date(2024, 2, 29),
                    None,
                ],
                dtype=object,
            )
        }
    )
    original = incoming.copy(deep=True)
    expected = ["2024-02-29", "2024-02-29", "2024-02-29", "2024-03-01", None, None]
    adapter = get_backend_adapter("trino")
    execute = adapter.execute_command
    statements: list[str] = []

    def record(connection: Any, statement: str) -> Any:
        statements.append(statement)
        return execute(connection, statement)

    # This alias has no Parquet staging; inserts use real Trino CAST parameters.
    for explicit in (True, False):
        options = {"table_schema": {"id": "DATE"}} if explicit else {}
        assert sql.load_df(
            _TARGET_ALIAS,
            table,
            incoming,
            write_mode="truncate_insert",
            trino_insert_chunk_size=2,
            retry_cnt=1,
            timeout_increment=0,
            query_label=label("date-dataframe-load"),
            **options,
        ) == len(incoming)
        pd.testing.assert_frame_equal(incoming, original)
        assert _rows(table) == expected
        assert sql.extract_ddl(_TARGET_ALIAS, table) == original_schema

        for bad in ("2024-02-30", "not-a-date"):
            invalid = incoming.copy(deep=True)
            invalid.loc[len(invalid) - 1, "id"] = bad
            original_invalid = invalid.copy(deep=True)
            statements.clear()
            with monkeypatch.context() as operations:
                operations.setattr(adapter, "execute_command", record)
                with pytest.raises(ValueError, match="DATE"):
                    sql.load_df(
                        _TARGET_ALIAS,
                        table,
                        invalid,
                        write_mode="truncate_insert",
                        trino_insert_chunk_size=2,
                        retry_cnt=1,
                        timeout_increment=0,
                        query_label=label("date-dataframe-invalid"),
                        **options,
                    )
            pd.testing.assert_frame_equal(invalid, original_invalid)
            assert _target_mutations(statements, table) == []
            assert _rows(table) == expected
            assert sql.extract_ddl(_TARGET_ALIAS, table) == original_schema

        duplicate = pd.DataFrame(
            {"id": pd.Series(["2024-02-29", pd.Timestamp("2024-02-29 12:00:00")], dtype=object)}
        )
        original_duplicate = duplicate.copy(deep=True)
        statements.clear()
        with monkeypatch.context() as operations:
            operations.setattr(adapter, "execute_command", record)
            with pytest.raises(ValueError, match="Duplicate key"):
                sql.load_df(
                    _TARGET_ALIAS,
                    table,
                    duplicate,
                    write_mode="append",
                    key_columns="id",
                    retry_cnt=1,
                    timeout_increment=0,
                    query_label=label("date-dataframe-keys"),
                    **options,
                )
        pd.testing.assert_frame_equal(duplicate, original_duplicate)
        assert _target_mutations(statements, table) == []
        assert _rows(table) == expected
        assert sql.extract_ddl(_TARGET_ALIAS, table) == original_schema


@pytest.mark.sql_scenario("audit.load.gp_date_values")
def test_gp_date_dataframe_loads_preserve_native_server_assignment_and_reject_invalid_values(
    monkeypatch: pytest.MonkeyPatch,
    resource_registry: ResourceRegistry,
) -> None:
    if not backend_enabled("gp"):
        pytest.skip("Greenplum requires x86_64")
    alias = "gp_target"
    table = _owned_target(resource_registry, purpose="gp_date_dataframe", backend="gp", alias=alias)
    original_schema = sql.extract_ddl(alias, table)
    aware = dt.datetime.fromisoformat("2024-03-01T00:30:00+14:00")
    # The driver's TIMESTAMPTZ parameter is assigned using this alias's native
    # session TimeZone. Derive the day from the engine rather than guessing it.
    native_day = sql.read(
        alias,
        "SELECT CAST(CAST(TIMESTAMPTZ '2024-03-01 00:30:00+14:00' AS DATE) AS VARCHAR)",
        output_type="scalar",
        retry_cnt=1,
        query_label=label("gp-date-native-reference"),
    )
    assert isinstance(native_day, str)
    incoming = pd.DataFrame(
        {
            "id": pd.Series(
                [
                    None,
                    "2024-02-29",
                    dt.datetime.fromisoformat("2024-02-29T12:34:56"),
                    pd.Timestamp("2024-02-29 21:30:45"),
                    aware,
                    dt.date(2024, 2, 29),
                    None,
                ],
                dtype=object,
            )
        }
    )
    original = incoming.copy(deep=True)
    expected = [*sorted(["2024-02-29"] * 4 + [native_day]), None, None]
    adapter = get_backend_adapter("gp")
    execute = adapter.execute_command
    statements: list[str] = []

    def record(connection: Any, statement: str) -> Any:
        statements.append(statement)
        return execute(connection, statement)

    for explicit in (True, False):
        options = {"table_schema": {"id": "DATE"}} if explicit else {}
        assert sql.load_df(
            alias,
            table,
            incoming,
            write_mode="truncate_insert",
            gp_insert_chunk_size=2,
            retry_cnt=1,
            timeout_increment=0,
            query_label=label("gp-date-dataframe-load"),
            **options,
        ) == len(incoming)
        pd.testing.assert_frame_equal(incoming, original)
        assert _rows(table, alias=alias) == expected
        assert sql.extract_ddl(alias, table) == original_schema

        for bad in ("2024-02-30", "not-a-date"):
            invalid = incoming.copy(deep=True)
            invalid.loc[len(invalid) - 1, "id"] = bad
            original_invalid = invalid.copy(deep=True)
            statements.clear()
            with monkeypatch.context() as operations:
                operations.setattr(adapter, "execute_command", record)
                with pytest.raises(ValueError, match=r"date|DATE"):
                    sql.load_df(
                        alias,
                        table,
                        invalid,
                        write_mode="truncate_insert",
                        gp_insert_chunk_size=2,
                        retry_cnt=1,
                        timeout_increment=0,
                        query_label=label("gp-date-dataframe-invalid"),
                        **options,
                    )
            pd.testing.assert_frame_equal(invalid, original_invalid)
            assert _target_mutations(statements, table, backend="gp") == []
            assert _rows(table, alias=alias) == expected
            assert sql.extract_ddl(alias, table) == original_schema
