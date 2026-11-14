"""Trino JSON string casts over actual SDK-formatted parameter literals."""

from __future__ import annotations

import json
from dataclasses import dataclass

from sqlglot import exp, parse_one

from tests.sql._support.trino_time_values import native_time_target


@dataclass(frozen=True)
class NativeJsonCast:
    wire: str
    json_text: str


def native_json_target(monkeypatch, expected_cast=None):
    database = native_time_target(monkeypatch, "JSON")
    database.table_rows[database.target] = ['"original"']

    def cast(wire):
        assert "CAST(? AS JSON)" in database.statements[-1][0]
        node = parse_one(wire, read="trino")
        if isinstance(node, exp.Null):
            return None
        if expected_cast is not None:
            assert wire == expected_cast.wire
            return expected_cast.json_text
        assert isinstance(node, exp.Literal)
        assert node.is_string
        # CAST(VARCHAR AS JSON) wraps a string; JSON_PARSE is a different operation.
        return json.dumps(node.this, ensure_ascii=False)

    monkeypatch.setattr(database, "decode_cast", cast)
    return database
