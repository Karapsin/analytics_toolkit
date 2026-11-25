"""Final native GP character assignment defines effective transfer key equality."""

from __future__ import annotations

from copy import deepcopy

import pytest
from analytics_toolkit import sql
from sqlglot import exp, parse_one

from tests.sql._support.assertions import strip_query_label
from tests.sql._support.gp_effective_keys import _assign_selected_value
from tests.sql._support.transfer_native_assignment import (
    NativeTransferConnection,
    NativeTransferDatabase,
    install_native_transfer,
)


@pytest.mark.parametrize("duplicate", [True, False], ids=["collision", "distinct"])
@pytest.mark.parametrize("source_backend", ["ch", "gp"])
def test_public_transfer_gp_keys_follow_final_native_character_assignment(
    monkeypatch, duplicate, source_backend
):
    values = [("a",), ("a " if duplicate else "b",)]
    original = deepcopy(values)
    assign = NativeTransferDatabase.assign
    close = NativeTransferConnection.close
    closed = []

    def close_connection(connection):
        closed.append(connection)
        close(connection)

    def native_assignment(value, native, *, explicit=False):
        kind = exp.DataType.build(native, dialect="postgres")
        if kind.this in {exp.DataType.Type.CHAR, exp.DataType.Type.VARCHAR}:
            return _assign_selected_value(value, native)
        return assign(value, native, explicit=explicit)

    monkeypatch.setattr(NativeTransferDatabase, "assign", staticmethod(native_assignment))
    monkeypatch.setattr(NativeTransferConnection, "close", close_connection)
    database = install_native_transfer(
        monkeypatch,
        rows=values,
        target_schema={"payload": "TEXT"},
        source_types={"payload": "String" if source_backend == "ch" else "VARCHAR(2)"},
        source_backend=source_backend,
        source_key="ch" if source_backend == "ch" else "gp_sandbox",
    )
    database.target.schema = {"payload": "CHARACTER(2)"}
    database.target.rows = [{"payload": "z "}]
    before = deepcopy(database.target.rows)
    execute = database.execute

    def native_execute(connection, statement, params=None):
        body = strip_query_label(statement)
        if not body.lstrip().upper().startswith("DELETE "):
            return execute(connection, statement, params)
        tree = parse_one(body, read="postgres")
        assert isinstance(tree, exp.Delete)
        assert database.identity(tree.this) == ("sandbox", "events")
        assert params is None
        database.commands.append(statement)
        database.live_mutations.append(statement)
        connection.begin_mutation()
        alias = tree.this.alias_or_name
        tables = list(tree.find_all(exp.Table))
        assert len(tables) == 2
        stage = tables[1]
        database.target.rows = [
            row
            for row in database.target.rows
            if not any(
                database.matches(
                    tree.args.get("where"),
                    {
                        **row,
                        **{f"{alias}.{name}": value for name, value in row.items()},
                        **{
                            f"{stage.alias_or_name}.{name}": value for name, value in source.items()
                        },
                    },
                )
                for source in database.table(stage).rows
            )
        ]
        return [], []

    monkeypatch.setattr(database, "execute", native_execute)

    def transfer():
        return sql.transfer(
            "ch" if source_backend == "ch" else "gp_sandbox",
            "gp",
            from_sql="SELECT payload FROM source",
            to_table="sandbox.events",
            table_schema={"payload": "VARCHAR(2)"},
            key_columns="payload",
            write_mode="upsert",
            ignore_source_staging=True,
            validate_row_count=False,
            retry_cnt=1,
            full_retry_cnt=1,
            timeout_increment=0,
            full_timeout_increment=0,
        )

    if duplicate:
        with pytest.raises(ValueError, match=r"[Uu]nique|[Dd]uplicate"):
            transfer()
        assert database.target.rows == before
        assert database.live_mutations == []
    else:
        assert transfer() == 2
        assert database.target.rows == [*before, {"payload": "a "}, {"payload": "b "}]
    assert database.target.schema == {"payload": "CHARACTER(2)"}
    assert values == original
    assert len(database.reads) == 1
    assert database.private_value_attempts
    assert database.opened
    assert len(closed) == len(database.opened)
    assert all(sum(item is connection for item in closed) == 1 for connection in database.opened)
