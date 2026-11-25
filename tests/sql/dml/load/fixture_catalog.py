"""Stateful service fakes must reject the same resource and chunk mistakes."""

from __future__ import annotations

import pytest
from psycopg2.extras import execute_values

from tests.sql._support.load_encoded_values import EncodedConnection
from tests.sql._support.load_table import FakeClickHouseClient


@pytest.mark.parametrize("table", ["`db`.`events`", "`d.b`.`event logs`", "`db`.`select`"])
def test_clickhouse_fixture_create_drop_exists_share_structured_identity(table: str) -> None:
    client = FakeClickHouseClient()
    client.command(f"CREATE TABLE {table} (id UInt8) ENGINE = Memory")
    assert client.query(f"EXISTS TABLE {table}").result_rows == [(1,)]
    client.command(f"DROP TABLE IF EXISTS {table}")
    assert client.query(f"EXISTS TABLE {table}").result_rows == [(0,)]
    assert not client.created_tables


def test_gp_catalog_fixture_consumes_only_each_insert_statement_bindings() -> None:
    connection = EncodedConnection(("not_installed_type",))
    cursor = connection.cursor()
    for table in ("public.first", "public.second"):
        cursor.execute(f"CREATE TABLE {table} (id BIGINT)")
    execute_values(cursor, "INSERT INTO public.first VALUES %s", [(1,), (2,)], page_size=1)
    execute_values(cursor, "INSERT INTO public.second VALUES %s", [(3,)], page_size=1)
    assert connection.table_rows[("public", "first")] == [(1,), (2,)]
    assert connection.table_rows[("public", "second")] == [(3,)]
    assert connection.bound_rows == [(1,), (2,), (3,)]
    cursor.close()


def test_gp_catalog_fixture_cannot_pretend_to_apply_an_unevaluated_insert_select() -> None:
    connection = EncodedConnection(("not_installed_type",))
    cursor = connection.cursor()
    with pytest.raises(NotImplementedError, match=r"INSERT.*SELECT"):
        cursor.execute("INSERT INTO public.target SELECT id FROM public.source")
    assert ("public", "target") not in connection.table_rows
    cursor.close()
