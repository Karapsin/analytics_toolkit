"""Structured token/AST handling for namespace commands unsupported by SQLGlot."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import sqlglot
import sqlparse
from sqlglot import exp
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers

_MIN_COMMAND_TOKENS = 3


@dataclass(frozen=True)
class CommandToken:
    text: str
    start: int
    end: int


def command_tokens(statement: str) -> list[CommandToken]:
    """Use SQLParse's lexer because SQLGlot collapses unsupported command tails."""
    result = []
    offset = 0
    for kind, text in sqlparse.lexer.tokenize(statement):  # type: ignore[no-untyped-call]
        if kind not in sqlparse.tokens.Whitespace and kind not in sqlparse.tokens.Comment:
            result.append(CommandToken(text, offset, offset + len(text) - 1))
        offset += len(text)
    return result


def _table(statement: str, tokens: list[Any], offset: int, dialect: str) -> tuple[Any, int]:
    end = offset + 1
    while end + 1 < len(tokens) and tokens[end].text == ".":
        end += 2
    value = sqlglot.parse_one(
        statement[tokens[offset].start : tokens[end - 1].end + 1], read=dialect, into=exp.Table
    )
    if dialect == "postgres":
        value = normalize_identifiers(value, dialect=dialect)
    return value, end


def _reference(table: Any, kind: str = "table") -> dict[str, Any]:
    return {
        "kind": kind,
        "catalog": table.catalog or None,
        "schema": table.db or None,
        "name": table.name,
    }


def _result(action: str, references: list[dict[str, Any]]) -> dict[str, Any]:
    return {"action": action, "objects": references, "targets": references, "parsed": True}


def _rename(statement: str, tokens: list[Any], dialect: str) -> dict[str, Any] | None:
    offset = 2
    references: list[dict[str, Any]] = []
    while offset < len(tokens):
        source, offset = _table(statement, tokens, offset, dialect)
        if offset >= len(tokens) or tokens[offset].text.upper() != "TO":
            return None
        target, offset = _table(statement, tokens, offset + 1, dialect)
        if not target.db:
            target.set("db", source.args.get("db"))
        references.extend((_reference(source), _reference(target)))
        if offset < len(tokens) and tokens[offset].text == ",":
            offset += 1
        else:
            break
    return _result("rename", references)


def _move_schema(statement: str, tokens: list[Any], dialect: str) -> dict[str, Any] | None:
    source, offset = _table(statement, tokens, 2, dialect)
    if [token.text.upper() for token in tokens[offset : offset + 2]] != ["SET", "SCHEMA"]:
        return None
    schema, _ = _table(statement, tokens, offset + 2, dialect)
    target = source.copy()
    target.set("db", schema.this)
    return _result("alter", [_reference(source), _reference(target)])


def namespace_command(statement: str, backend: str) -> dict[str, Any] | None:
    dialect = {"gp": "postgres", "ch": "clickhouse", "trino": "trino"}[backend]
    tokens = command_tokens(statement)
    if (
        len(tokens) > 1
        and tokens[0].text.upper() == "SET"
        and tokens[1].text.upper()
        in {
            "LOCAL",
            "SESSION",
        }
    ):
        tokens = [tokens[0], *tokens[2:]]
    if len(tokens) < _MIN_COMMAND_TOKENS:
        return None
    action, noun = tokens[0].text.upper(), tokens[1].text.upper()
    if action in {"CREATE", "DROP"} and noun == "CATALOG":
        offset = 2
        # SQLParse releases differ in whether IF [NOT] EXISTS is one keyword.
        while tokens[offset].text.upper() in {
            "IF",
            "NOT",
            "EXISTS",
            "IF EXISTS",
            "NOT EXISTS",
            "IF NOT EXISTS",
        }:
            offset += 1
        table, _ = _table(statement, tokens, offset, dialect)
        return _result(action.lower(), [_reference(table, "catalog")])
    if action == "RENAME" and noun == "TABLE":
        return _rename(statement, tokens, dialect)
    if action == "ALTER" and noun == "TABLE":
        return _move_schema(statement, tokens, dialect)
    if action == "SET" and noun == "SEARCH_PATH" and tokens[2].text.upper() in {"TO", "="}:
        values = sqlglot.parse_one("SELECT " + statement[tokens[3].start :], read=dialect)
        return {
            "action": "set",
            "objects": [],
            "parsed": True,
            "namespace": {"search_path": [value.name for value in values.expressions]},
        }
    return None
