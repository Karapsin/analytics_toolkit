"""Native integer text assignments retain each authoritative destination width."""

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
from tests.sql.integration.manifest import scenario_param

if TYPE_CHECKING:
    from typing import Any

    from tests.sql.integration._support.resources import ResourceRegistry

pytestmark = [pytest.mark.integration, pytest.mark.integration_core]


def _owned_table(registry: ResourceRegistry, backend: str, native: str) -> tuple[str, str]:
    alias = "gp_target" if backend == "gp" else "trino_target_values"
    allocated = integration_table(backend, f"{native.lower()}_text")
    prefix = None
    if backend == "gp":
        table, properties = allocated, " DISTRIBUTED RANDOMLY"
    else:
        # Iceberg cannot persist TINYINT/SMALLINT. Hive retains the declared
        # width; the table and external files both belong to this run.
        relation = TableIdentifier.parse(allocated, "trino").relation
        table = TableIdentifier(("hive", "default", relation), (False, False, False)).render(
            "trino"
        )
        prefix = f"integer-text/{relation}/"
        location = exp.Literal.string(f"s3a://warehouse/{prefix}").sql(dialect="trino")
        properties = f" WITH (format = 'PARQUET', external_location = {location})"
    acknowledged = False

    def cleanup() -> None:
        if acknowledged:
            sql.execute(
                alias,
                f"DROP TABLE IF EXISTS {table}",
                retry_cnt=1,
                query_label=label("integer-text-cleanup"),
            )

    registry.finalizer(cleanup)
    sql.execute(
        alias,
        f"CREATE TABLE {table} (payload {native}){properties}",
        retry_cnt=1,
        query_label=label("integer-text-create"),
    )
    acknowledged = True
    if prefix is not None:
        registry.minio(prefix)
    return alias, table


def _native_type(alias: str, table: str, backend: str) -> str:
    parts = TableIdentifier.parse(table, backend).parts
    source = exp.Table(
        this=exp.to_identifier("columns"),
        db=exp.to_identifier("information_schema"),
        catalog=exp.to_identifier(parts[-3]) if len(parts) == 3 else None,
    )
    statement = (
        exp.select("data_type")
        .from_(source)
        .where(
            exp.column("table_schema").eq(exp.Literal.string(parts[-2])),
            exp.column("table_name").eq(exp.Literal.string(parts[-1])),
            exp.column("column_name").eq(exp.Literal.string("payload")),
        )
    )
    return sql.read(
        alias,
        statement.sql(dialect="postgres" if backend == "gp" else "trino"),
        output_type="scalar",
        retry_cnt=1,
        query_label=label("integer-text-native-type"),
    )


def _rows(alias: str, table: str) -> list[int | None]:
    frame = sql.read(
        alias,
        f"SELECT payload FROM {table} ORDER BY payload NULLS LAST",
        retry_cnt=1,
        query_label=label("integer-text-rows"),
    )
    return [None if pd.isna(value) else int(value) for value in frame["payload"]]


def _target_mutations(statements: list[str], table: str, backend: str) -> list[str]:
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


@pytest.mark.parametrize(
    ("backend", "native", "bits"),
    [
        scenario_param("audit.load.gp_smallint_text", "gp", "SMALLINT", 16),
        scenario_param("audit.load.gp_integer_text", "gp", "INTEGER", 32),
        scenario_param("audit.load.gp_bigint_text", "gp", "BIGINT", 64),
        scenario_param("audit.load.trino_tinyint_text", "trino", "TINYINT", 8),
        scenario_param("audit.load.trino_smallint_text", "trino", "SMALLINT", 16),
        scenario_param("audit.load.trino_integer_text", "trino", "INTEGER", 32),
    ],
)
def test_integer_text_loads_preserve_authoritative_width_and_reject_invalid_values(
    monkeypatch: pytest.MonkeyPatch,
    resource_registry: ResourceRegistry,
    backend: str,
    native: str,
    bits: int,
) -> None:
    if not backend_enabled(backend):
        pytest.skip("Greenplum requires x86_64")
    alias, table = _owned_table(resource_registry, backend, native)
    assert _native_type(alias, table, backend) == native.lower()
    original_schema = sql.extract_ddl(alias, table)
    lower, upper = -(2 ** (bits - 1)), 2 ** (bits - 1) - 1
    incoming = pd.DataFrame(
        {
            "payload": pd.Series(
                [
                    str(lower),
                    str(upper),
                    "0",
                    "-0",
                    "+0",
                    "00042",
                    "-00007",
                    " \t+13\r\n",
                    None,
                ],
                dtype=object,
            )
        }
    )
    original = incoming.copy(deep=True)
    loaded: list[int | None] = []
    adapter = get_backend_adapter(backend)
    execute = adapter.execute_command
    statements: list[str] = []

    def record(connection: Any, statement: str) -> Any:
        statements.append(statement)
        return execute(connection, statement)

    # Hive preserves these widths, but its nontransactional DELETE support is
    # connector-specific. Append proves the native cast without claiming clear.
    mode = "truncate_insert" if backend == "gp" else "append"
    for explicit in (True, False):
        options = {"table_schema": {"payload": native}} if explicit else {}
        assert sql.load_df(
            alias,
            table,
            incoming,
            write_mode=mode,
            retry_cnt=1,
            timeout_increment=0,
            query_label=label("integer-text-load"),
            **options,
        ) == len(incoming)
        expected_batch = [lower, -7, 0, 0, 0, 13, 42, upper, None]
        loaded = expected_batch if backend == "gp" else [*loaded, *expected_batch]
        expected = [
            *sorted(value for value in loaded if value is not None),
            *[None for value in loaded if value is None],
        ]
        pd.testing.assert_frame_equal(incoming, original)
        assert _rows(alias, table) == expected
        assert _native_type(alias, table, backend) == native.lower()
        assert sql.extract_ddl(alias, table) == original_schema

        for bad in ("private-invalid-integer", "1.5", "1_000", str(lower - 1), str(upper + 1)):
            invalid = incoming.copy(deep=True)
            invalid.loc[len(invalid) - 1, "payload"] = bad
            original_invalid = invalid.copy(deep=True)
            statements.clear()
            with monkeypatch.context() as operations:
                operations.setattr(adapter, "execute_command", record)
                with pytest.raises(ValueError, match=r"(?i)int|range") as error:
                    sql.load_df(
                        alias,
                        table,
                        invalid,
                        write_mode=mode,
                        retry_cnt=1,
                        timeout_increment=0,
                        query_label=label("integer-text-invalid"),
                        **options,
                    )
            assert "private-invalid-integer" not in str(error.value)
            pd.testing.assert_frame_equal(invalid, original_invalid)
            assert _target_mutations(statements, table, backend) == []
            assert _rows(alias, table) == expected
            assert _native_type(alias, table, backend) == native.lower()
            assert sql.extract_ddl(alias, table) == original_schema
