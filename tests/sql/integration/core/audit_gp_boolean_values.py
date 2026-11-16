"""Greenplum BOOLEAN text assignments preserve native false and prefix rules."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.core.identifiers import TableIdentifier
from sqlglot import exp, parse_one

from tests.sql.integration._support.backends import backend_enabled, integration_table
from tests.sql.integration._support.reconfiguration import label

if TYPE_CHECKING:
    from typing import Any

    from tests.sql.integration._support.resources import ResourceRegistry

pytestmark = [pytest.mark.integration, pytest.mark.integration_core]
_ALIAS = "gp_target"


def _owned_table(registry: ResourceRegistry) -> str:
    table = integration_table("gp", "boolean_native_text")
    acknowledged = False

    def cleanup() -> None:
        if acknowledged:
            sql.execute(
                _ALIAS,
                f"DROP TABLE IF EXISTS {table}",
                retry_cnt=1,
                query_label=label("gp-boolean-cleanup"),
            )

    registry.finalizer(cleanup)
    sql.execute(
        _ALIAS,
        f"CREATE TABLE {table} (payload BOOLEAN) DISTRIBUTED RANDOMLY",
        retry_cnt=1,
        query_label=label("gp-boolean-create"),
    )
    acknowledged = True
    return table


def _rows(table: str) -> list[bool | None]:
    frame = sql.read(
        _ALIAS,
        f"SELECT payload FROM {table} ORDER BY payload NULLS LAST",
        retry_cnt=1,
        query_label=label("gp-boolean-rows"),
    )
    return [None if pd.isna(value) else bool(value) for value in frame["payload"]]


def _target_mutations(statements: list[str], table: str) -> list[str]:
    expected = TableIdentifier.parse(table, "gp").parts
    mutations = []
    for statement in statements:
        tree = parse_one(statement, read="postgres")
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


@pytest.mark.sql_scenario("audit.load.gp_boolean_values")
def test_gp_boolean_text_loads_preserve_native_false_and_reject_invalid_values(
    monkeypatch: pytest.MonkeyPatch,
    resource_registry: ResourceRegistry,
) -> None:
    if not backend_enabled("gp"):
        pytest.skip("Greenplum requires x86_64")
    table = _owned_table(resource_registry)
    original_schema = sql.extract_ddl(_ALIAS, table)
    cases = [
        ("true", True),
        ("false", False),
        ("TRUE", True),
        ("FaLsE", False),
        ("t", True),
        ("tr", True),
        ("tru", True),
        ("y", True),
        ("ye", True),
        ("yes", True),
        ("on", True),
        ("1", True),
        ("f", False),
        ("fa", False),
        ("fal", False),
        ("fals", False),
        ("n", False),
        ("no", False),
        ("of", False),
        ("off", False),
        ("0", False),
        ("\t false \r\n", False),
        ("\vye\f", True),
        (True, True),
        (False, False),
        (None, None),
    ]
    incoming = pd.DataFrame(
        {"payload": pd.Series([value for value, _expected in cases], dtype=object)}
    )
    original = incoming.copy(deep=True)
    expected = [*sorted(value for _raw, value in cases if value is not None), None]
    adapter = get_backend_adapter("gp")
    execute = adapter.execute_command
    statements: list[str] = []

    def record(connection: Any, statement: str) -> Any:
        statements.append(statement)
        return execute(connection, statement)

    for explicit in (True, False):
        options = {"table_schema": {"payload": "BOOLEAN"}} if explicit else {}
        assert sql.load_df(
            _ALIAS,
            table,
            incoming,
            write_mode="truncate_insert",
            gp_insert_chunk_size=8,
            retry_cnt=1,
            timeout_increment=0,
            query_label=label("gp-boolean-load"),
            **options,
        ) == len(incoming)
        pd.testing.assert_frame_equal(incoming, original)
        assert _rows(table) == expected
        assert sql.extract_ddl(_ALIAS, table) == original_schema

        for bad in ("private-invalid-boolean", "o", "truex", "f alse", "\u00a0false"):
            invalid = incoming.copy(deep=True)
            invalid.loc[len(invalid) - 1, "payload"] = bad
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
                        gp_insert_chunk_size=8,
                        retry_cnt=1,
                        timeout_increment=0,
                        query_label=label("gp-boolean-invalid"),
                        **options,
                    )
            assert "private-invalid-boolean" not in str(error.value)
            pd.testing.assert_frame_equal(invalid, original_invalid)
            assert _target_mutations(statements, table) == []
            assert _rows(table) == expected
            assert sql.extract_ddl(_ALIAS, table) == original_schema
