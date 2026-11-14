"""Public native-assignment loads against supported Greenplum 6 types.

GP6/PostgreSQL 9.4 does not support arrays of domains. This scenario uses a
scalar domain and native CIDR[]; newer-server domain-array truncation is covered
by the paired fast public/catalog regressions, without unsupported GP6 DDL.
"""

from __future__ import annotations

import os
import uuid
from typing import TYPE_CHECKING

import pandas as pd
import pytest
import sqlparse
from analytics_toolkit import sql
from analytics_toolkit.sql.backends import get_backend_adapter
from sqlglot import exp, parse_one

from tests.sql.integration._support.backends import backend_enabled
from tests.sql.integration._support.identity import query_label, resource_name

if TYPE_CHECKING:
    from typing import Any

    from tests.sql.integration._support.resources import ResourceRegistry

pytestmark = [pytest.mark.integration, pytest.mark.integration_core]


def _identity(purpose: str) -> str:
    return resource_name(
        os.environ.get("SQL_INTEGRATION_RUN_ID", uuid.uuid4().hex[:8]),
        os.environ.get("SQL_INTEGRATION_TEST_ID", "manual"),
        purpose,
    )


def _label(purpose: str) -> str:
    return query_label(
        os.environ.get("SQL_INTEGRATION_RUN_ID", uuid.uuid4().hex[:8]),
        os.environ.get("SQL_INTEGRATION_TEST_ID", "manual"),
        purpose,
    )


def _owned_domain(alias: str, registry: ResourceRegistry) -> tuple[str, str]:
    schema = _identity("assignment_types")
    owned = False

    def cleanup() -> None:
        if owned:
            sql.execute(
                alias,
                f'DROP SCHEMA IF EXISTS "{schema}" CASCADE',
                retry_cnt=1,
                query_label=_label("gp-assignment-domain-cleanup"),
            )

    registry.finalizer(cleanup)
    sql.execute(
        alias,
        f'CREATE SCHEMA "{schema}"',
        retry_cnt=1,
        query_label=_label("gp-assignment-schema-create"),
    )
    owned = True
    domain = f'"{schema}"."value%type"'
    sql.execute(
        alias,
        f"CREATE DOMAIN {domain} AS VARCHAR(3)",
        retry_cnt=1,
        query_label=_label("gp-assignment-domain-create"),
    )
    return schema, domain


def _rows(alias: str, table: str) -> list[tuple[Any, ...]]:
    frame = sql.read(
        alias,
        "SELECT row_id, network::text, device::text, code::text, "
        f"prefixes::text[], prefixes IS NULL FROM {table} ORDER BY row_id",
        retry_cnt=1,
        query_label=_label("gp-assignment-read"),
    )
    rows = []
    for row_id, network, device, code, prefixes, missing in frame.itertuples(
        index=False, name=None
    ):
        if bool(missing):
            assert pd.isna(prefixes)
            items = None
        else:
            assert isinstance(prefixes, list)
            items = prefixes
        fields = tuple(None if pd.isna(value) else str(value) for value in (network, device, code))
        rows.append((int(row_id), *fields, items))
    return rows


def _incoming() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "row_id": [1, 2, 3],
            "network": ["192.0.2.0/24", "2001:db8::/32", None],
            "device": ["08:00:2B:01:02:03", None, "08:00:2b:01:02:04"],
            "code": pd.Series(["abc", None, ""], dtype=object),
            "prefixes": pd.Series([["192.0.2.0/24", None], [], None], dtype=object),
        }
    )


def _live_mutations(statements: list[str], schema: str, name: str) -> list[str]:
    mutations = []
    for statement in statements:
        parsed = sqlparse.parse(statement)
        if not parsed or parsed[0].get_type() not in {
            "DELETE",
            "INSERT",
            "UPDATE",
            "DROP",
            "ALTER",
            "TRUNCATE",
        }:
            continue
        tree = parse_one(statement, read="postgres")
        if isinstance(
            tree, (exp.Delete, exp.Insert, exp.Update, exp.Drop, exp.Alter, exp.TruncateTable)
        ) and any(table.db == schema and table.name == name for table in tree.find_all(exp.Table)):
            mutations.append(statement)
    return mutations


def _load(alias: str, table: str, frame: pd.DataFrame, **options: Any) -> int:
    return sql.load_df(
        alias,
        table,
        frame,
        retry_cnt=1,
        gp_distributed_by_key="row_id",
        query_label=_label("gp-assignment-load"),
        **options,
    )


@pytest.mark.sql_scenario("audit.load.gp_native_assignment")
def test_greenplum_native_assignment_certifies_cidr_and_scalar_domain_before_clear(
    monkeypatch: pytest.MonkeyPatch,
    resource_registry: ResourceRegistry,
) -> None:
    if not backend_enabled("gp"):
        pytest.skip("Greenplum requires x86_64")
    alias = "gp_target"
    schema, domain = _owned_domain(alias, resource_registry)
    name = _identity("assignment_existing")
    existing = resource_registry.table(alias, f'"{schema}"."{name}"')
    explicit = resource_registry.table(alias, f'"{schema}"."{_identity("assignment_explicit")}"')
    types = {
        "row_id": "BIGINT",
        "network": "CIDR",
        "device": "MACADDR",
        "code": domain,
        "prefixes": "CIDR[]",
    }
    sql.execute(
        alias,
        f"CREATE TABLE {existing} (row_id BIGINT, network CIDR, device MACADDR, "
        f"code {domain}, prefixes CIDR[]) DISTRIBUTED BY (row_id)",
        retry_cnt=1,
        query_label=_label("gp-assignment-existing-create"),
    )
    sql.execute(
        alias,
        f"INSERT INTO {existing} VALUES "
        "(99, CIDR '198.51.100.0/24', MACADDR '08:00:2b:01:02:ff', 'old', "
        "ARRAY[CIDR '198.51.100.0/24'])",
        retry_cnt=1,
        query_label=_label("gp-assignment-seed"),
    )
    seed = [(99, "198.51.100.0/24", "08:00:2b:01:02:ff", "old", ["198.51.100.0/24"])]
    assert _rows(alias, existing) == seed
    adapter = get_backend_adapter("gp")
    execute_command = adapter.execute_command
    statements: list[str] = []

    def record(connection: Any, statement: str) -> Any:
        statements.append(statement)
        return execute_command(connection, statement)

    invalid = _incoming().iloc[:2].copy(deep=True)
    invalid["code"] = pd.Series(["ok", "long"], dtype=object)
    with monkeypatch.context() as operations:
        operations.setattr(adapter, "execute_command", record)
        with pytest.raises(ValueError, match=r"Greenplum native assignment.*failed"):
            _load(alias, existing, invalid, write_mode="truncate_insert")
        assert _live_mutations(statements, schema, name) == []
        assert _rows(alias, existing) == seed
        assert _load(alias, existing, _incoming(), write_mode="truncate_insert") == 3
        assert _live_mutations(statements, schema, name)
    assert _load(alias, explicit, _incoming(), write_mode="replace", table_schema=types) == 3
    expected = [
        (1, "192.0.2.0/24", "08:00:2b:01:02:03", "abc", ["192.0.2.0/24", None]),
        (2, "2001:db8::/32", None, None, []),
        (3, None, "08:00:2b:01:02:04", "", None),
    ]
    literal_domain = adapter.render_transfer_literal(domain)
    for table in (existing, explicit):
        assert _rows(alias, table) == expected
        assert sql.read(
            alias,
            f"SELECT pg_typeof(code) = {literal_domain}::regtype FROM {table} LIMIT 1",
            output_type="scalar",
            retry_cnt=1,
            query_label=_label("gp-assignment-domain-type"),
        )
