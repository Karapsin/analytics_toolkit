"""Public dataframe loads with real GP SQL rendering and psycopg adaptation."""

from __future__ import annotations

from typing import Any

from psycopg2.extensions import QuotedString, adapt
from sqlglot import exp, parse_one
from sqlparse import parse
from sqlparse.sql import Parenthesis

from tests.sql._support.created_gp_relations import locked_relation
from tests.sql._support.fakes import FakeDbapiConnection, FakeDbapiCursor
from tests.sql._support.load_table import load_df_module


class _EncodedCursor(FakeDbapiCursor):
    def mogrify(self, template: bytes | str, values: Any) -> bytes:
        if isinstance(template, str):
            template = template.encode("utf-8")
        self.connection.bound_rows.append(tuple(values))
        if (pending := getattr(self.connection, "pending_bound_rows", None)) is not None:
            pending.append(tuple(values))
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
        connection = self.connection
        if "pg_catalog.pg_class" in text and "c.relfilenode" in text:
            node = parse_one(f"SELECT * FROM {params[0]}", read="postgres").args["from_"].this
            identity = (node.db, node.name)
            oid = connection.relation_oids.get(identity)
            self._rows = (
                [] if oid is None else [("fake", "127.0.0.1", 5432, oid, oid + 100000, 2200)]
            )
            return
        locked = locked_relation(text)
        if locked is not None:
            assert (locked.db, locked.name) in connection.relation_oids
            self._rows = []
            return
        if "current_setting" in text.lower():
            tree = parse_one(text, read="postgres")
            names = [
                node.expressions[0].this.lower()
                for node in tree.find_all(exp.Anonymous)
                if node.name.lower() == "current_setting"
            ]
            self._rows = [tuple(connection.settings[name] for name in names)]
            return
        connection.apply_catalog_statement(text)


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
        self.pending_bound_rows: list[tuple[Any, ...]] = []
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

        self.relation_oids = {
            identity: 10000 + index for index, identity in enumerate(self.table_rows)
        }
        self.next_relation_oid = 10002
        self.settings = {
            "timezone": "UTC",
            "datestyle": "ISO, MDY",
            "timezone_abbreviations": "Default",
            "intervalstyle": "postgres",
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
            assert identity not in self.relation_oids
            self.next_relation_oid += 1
            self.relation_oids[identity] = self.next_relation_oid
            self.table_rows[identity] = []
        elif isinstance(statement, (exp.Delete, exp.TruncateTable)):
            self.table_rows[identity] = []
        elif isinstance(statement, exp.Insert):
            if not isinstance(statement.expression, exp.Values):
                message = "Fixture cannot evaluate INSERT SELECT without a relational source model."
                raise NotImplementedError(message)
            rows, self.pending_bound_rows = self.pending_bound_rows, []
            assert len(rows) == len(statement.expression.expressions)
            self.table_rows.setdefault(identity, []).extend(rows)
        elif isinstance(statement, exp.Drop):
            self.table_rows.pop(identity, None)
            self.relation_oids.pop(identity, None)

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
    if not exists:
        connection.table_rows.pop(("public", "encoded_target"))
        connection.relation_oids.pop(("public", "encoded_target"))
    monkeypatch.setattr(load_df_module, "get_sql_connection", lambda _key: connection)
    monkeypatch.setattr(load_df_module, "table_exists", lambda *_args, **_kwargs: exists)
    monkeypatch.setattr(load_df_module, "get_table_column_types", lambda *_args, **_kwargs: schema)
    return connection
