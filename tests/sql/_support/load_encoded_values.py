"""Public dataframe loads with real GP SQL rendering and psycopg adaptation."""

from __future__ import annotations

from typing import Any

from psycopg2.extensions import QuotedString, adapt

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
        super().execute(sql.decode("utf-8") if isinstance(sql, bytes) else sql, params)


class EncodedConnection(FakeDbapiConnection):
    encoding = "UTF8"

    def __init__(self) -> None:
        super().__init__()
        self.bound_rows: list[tuple[Any, ...]] = []

    def cursor(self) -> _EncodedCursor:
        return _EncodedCursor(self)


def configure_load(monkeypatch, write_sql_connections, *, exists, schema):
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
    connection = EncodedConnection()
    monkeypatch.setattr(load_df_module, "get_sql_connection", lambda _key: connection)
    monkeypatch.setattr(load_df_module, "table_exists", lambda *_args, **_kwargs: exists)
    monkeypatch.setattr(load_df_module, "get_table_column_types", lambda *_args, **_kwargs: schema)
    return connection
