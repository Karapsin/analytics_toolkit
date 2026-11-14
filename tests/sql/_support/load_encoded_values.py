"""Public dataframe loads with real GP SQL rendering and psycopg adaptation."""

from __future__ import annotations

from typing import Any

from psycopg2.extensions import QuotedString, adapt
from sqlglot import exp, parse_one
from sqlparse import parse
from sqlparse.sql import Parenthesis

from tests.sql._support.fakes import FakeDbapiConnection, FakeDbapiCursor
from tests.sql._support.load_table import load_df_module


class _EncodedCursor(FakeDbapiCursor):
    def mogrify(self, template: bytes | str, values: Any) -> bytes:
        if isinstance(template, str):
            template = template.encode("utf-8")
        self.connection.bound_rows.append(tuple(values))
        quoted = []
        for value in values:
            adapter = adapt(value)
            if isinstance(adapter, QuotedString):
                adapter.encoding = "utf-8"
            quoted.append(adapter.getquoted())
        return template % tuple(quoted)

    def execute(self, sql: bytes | str, params: tuple[Any, ...] | None = None) -> None:
        text = sql.decode("utf-8") if isinstance(sql, bytes) else sql
        super().execute(text, params)
        if self.connection.missing_type_declarations:
            self.connection.apply_catalog_statement(text)


class _UndefinedTypeError(RuntimeError):
    pgcode = "42704"


def _catalog_statement(sql: str) -> exp.Expression:
    parsed = parse(sql)[0]
    if parsed.get_type() == "CREATE":
        # The column definition is ordinary PostgreSQL. Greenplum's later
        # WITH/DISTRIBUTED properties are irrelevant to type lookup and are
        # outside sqlglot's PostgreSQL CREATE grammar.
        tokens = []
        for token in parsed.tokens:
            tokens.append(str(token))
            if isinstance(token, Parenthesis):
                break
        sql = "".join(tokens)
    return parse_one(sql, read="postgres")


class EncodedConnection(FakeDbapiConnection):
    encoding = "UTF8"

    def __init__(self, missing_types: tuple[str, ...] = ()) -> None:
        super().__init__()
        self.bound_rows: list[tuple[Any, ...]] = []
        self.missing_type_declarations = [
            (
                native,
                parse_one(f"CREATE TABLE _catalog_probe (value {native})", read="postgres")
                .this.expressions[0]
                .args["kind"],
            )
            for native in missing_types
        ]
        self.rejected_type_declarations: list[str] = []
        self.catalog_statements: list[exp.Expression] = []
        self.table_rows = {
            ("public", "encoded_target"): [(41,)],
            ("public", "unrelated_resource"): [(99,)],
        }

    def apply_catalog_statement(self, sql: str) -> None:
        statement = _catalog_statement(sql)
        self.catalog_statements.append(statement)
        table = statement.find(exp.Table)
        if table is None:
            return
        identity = (table.db, table.name)
        if isinstance(statement, exp.Create):
            for column in statement.find_all(exp.ColumnDef):
                for native, missing in self.missing_type_declarations:
                    if column.args.get("kind") == missing:
                        self.rejected_type_declarations.append(native)
                        message = "Requested type does not exist in the fake catalog."
                        raise _UndefinedTypeError(message)
            self.table_rows[identity] = []
        elif isinstance(statement, (exp.Delete, exp.TruncateTable)):
            self.table_rows[identity] = []
        elif isinstance(statement, exp.Insert):
            self.table_rows.setdefault(identity, []).extend(self.bound_rows)
        elif isinstance(statement, exp.Drop):
            self.table_rows.pop(identity, None)

    def cursor(self) -> _EncodedCursor:
        return _EncodedCursor(self)


def configure_load(monkeypatch, write_sql_connections, *, exists, schema, missing_types=()):
    write_sql_connections(
        {
            "encoded_gp": {
                "type": "gp",
                "host": "fake.invalid",
                "port": 5432,
                "database": "fake",
                "user": "fake",
                "password": "fake",
            }
        }
    )
    connection = EncodedConnection(missing_types)
    monkeypatch.setattr(load_df_module, "get_sql_connection", lambda _key: connection)
    monkeypatch.setattr(load_df_module, "table_exists", lambda *_args, **_kwargs: exists)
    monkeypatch.setattr(load_df_module, "get_table_column_types", lambda *_args, **_kwargs: schema)
    return connection
