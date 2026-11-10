"""Accepted Trino chunks may repeat when the public load operation retries."""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

import pandas as pd
from analytics_toolkit import sql

if TYPE_CHECKING:
    import pytest

load_module = importlib.import_module("analytics_toolkit.sql.dml.load.load_df")


def test_trino_partial_chunk_replay_is_explicit_at_least_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accepted: list[int] = []
    submissions = 0
    connections: list[Any] = []

    class Cursor:
        def execute(self, _query: str, params: list[int]) -> None:
            nonlocal submissions
            submissions += 1
            accepted.extend(params)
            if submissions == 2:
                message = "lost acknowledgement on later chunk"
                raise ConnectionError(message)

        def close(self) -> None:
            pass

    class Connection:
        closed = 0

        def cursor(self) -> Cursor:
            return Cursor()

        def close(self) -> None:
            self.closed += 1

    def open_connection(_key: str) -> Connection:
        connection = Connection()
        connections.append(connection)
        return connection

    monkeypatch.setattr(load_module, "get_sql_connection", open_connection)
    monkeypatch.setattr(load_module, "table_exists", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        load_module, "get_table_column_types", lambda *_args, **_kwargs: {"id": "BIGINT"}
    )
    monkeypatch.setattr(load_module, "analyze_table", lambda *_args, **_kwargs: None)
    result = sql.load_df(
        "trino",
        "sandbox.target",
        pd.DataFrame({"id": [1, 2, 3]}),
        append=True,
        table_schema={"id": "BIGINT"},
        trino_insert_chunk_size=1,
        retry_cnt=2,
        timeout_increment=0,
    )
    assert result == 3
    assert accepted == [1, 2, 1, 2, 3]
    assert all(connection.closed == 1 for connection in connections)
