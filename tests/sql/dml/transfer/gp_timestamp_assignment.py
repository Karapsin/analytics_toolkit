"""Temporal text uses a real owned GP assignment before transfer publication."""

from __future__ import annotations

import datetime as dt

from analytics_toolkit import sql
from sqlglot import exp, parse_one

from tests.sql._support.created_gp_relations import locked_relation
from tests.sql._support.transfer_native_assignment import (
    NativeTransferDatabase,
    install_native_transfer,
)


def test_public_gp_timestamp_text_transfer_certifies_native_assignment_before_target_mutation(
    monkeypatch,
):
    assign = NativeTransferDatabase.assign

    def native_assignment(value, native, *, explicit=False):
        if native.upper().startswith("TIMESTAMP"):
            if value is None:
                return None
            parsed = dt.datetime.fromisoformat(value) if isinstance(value, str) else value
            assert isinstance(parsed, dt.datetime)
            assert parsed.tzinfo is None
            return parsed
        return assign(value, native, explicit=explicit)

    monkeypatch.setattr(NativeTransferDatabase, "assign", staticmethod(native_assignment))
    text = "2025-01-02 03:04:05.123456"
    database = install_native_transfer(
        monkeypatch,
        rows=[(text,)],
        target_schema={"payload": "TEXT"},
        source_types={"payload": "String"},
    )
    database.target.schema = {"payload": "TIMESTAMP"}
    database.target.rows = [{"payload": dt.datetime.fromisoformat("2001-01-01T00:00:00.000000")}]
    metadata = database.metadata
    contexts = []
    settings = {
        "timezone": "Europe/Berlin",
        "datestyle": "ISO, MDY",
        "timezone_abbreviations": "Default",
        "intervalstyle": "postgres",
    }

    def native_settings(statement, params):
        if "current_setting" in statement:
            tree = parse_one(statement, read="postgres")
            names = [node.this.lower() for node in tree.find_all(exp.Literal) if node.is_string]
            contexts.append(tuple(names))
            return [tuple(settings[name] for name in names)]
        return metadata(statement, params)

    monkeypatch.setattr(database, "metadata", native_settings)

    result = sql.transfer(
        "ch",
        "gp",
        from_sql="SELECT payload FROM source",
        to_table="sandbox.events",
        table_schema={"payload": "TIMESTAMP"},
        write_mode="truncate_insert",
        ignore_source_staging=True,
        validate_row_count=False,
        retry_cnt=1,
        full_retry_cnt=1,
        timeout_increment=0,
        full_timeout_increment=0,
    )

    assert result == 1
    assert database.target.schema == {"payload": "TIMESTAMP"}
    assert database.target.rows == [
        {"payload": dt.datetime.fromisoformat("2025-01-02T03:04:05.123456")}
    ]
    assert contexts
    assert database.private_value_attempts
    private = set(database.private_value_attempts)
    assert ("sandbox", "events") not in private
    statements = [
        None
        if locked_relation(sql) is not None
        else parse_one(database._ddl_schema_prefix(sql), read="postgres")
        for sql in database.commands
    ]
    typed_creates = [
        index
        for index, statement in enumerate(statements)
        if isinstance(statement, exp.Create)
        and database.identity(statement.this) in private
        and statement.this.expressions[0].kind.sql(dialect="postgres").startswith("TIMESTAMP")
    ]
    clear_index = next(
        index
        for index, statement in enumerate(statements)
        if isinstance(statement, exp.TruncateTable)
    )
    assert typed_creates
    assert max(typed_creates) < clear_index
    populations = [
        index
        for index, statement in enumerate(statements)
        if isinstance(statement, exp.Insert)
        and isinstance(statement.expression, exp.Values)
        and database.identity(statement.this) in private
    ]
    assert populations
    assert max(populations) < clear_index
    assert len(database.reads) == database.source_opens == 1
    assert set(database.tables) == {("sandbox", "events")}
