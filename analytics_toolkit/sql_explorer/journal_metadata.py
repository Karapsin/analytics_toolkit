"""Best-effort SQL action and object metadata for journal records."""

from __future__ import annotations

from typing import Any, cast

import sqlglot
import sqlparse
from sqlglot import exp
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers
from sqlglot.optimizer.scope import Scope, traverse_scope

from analytics_toolkit._sql_statements import split_statements

from .journal_namespace_commands import command_tokens, namespace_command

_DIALECTS = {"gp": "postgres", "trino": "trino", "ch": "clickhouse"}
_MIN_SHOW_NAMESPACE_TOKENS = 4


def _show_namespace(statement: str, backend: str) -> dict[str, Any] | None:
    """Handle SHOW namespace forms represented by SQLGlot as generic commands."""
    dialect = _DIALECTS.get(backend, backend)
    tokens = command_tokens(statement)
    if len(tokens) < _MIN_SHOW_NAMESPACE_TOKENS or tokens[0].text.upper() != "SHOW":
        return None
    noun = tokens[1].text.upper()
    if noun not in {"SCHEMAS", "TABLES", "VIEWS"} or tokens[2].text.upper() not in {"FROM", "IN"}:
        return None
    stop = next(
        (
            token.start
            for token in tokens[3:]
            if token.text.upper() in {"LIKE", "ILIKE", "WHERE", "LIMIT"}
        ),
        len(statement),
    )
    table = sqlglot.parse_one(statement[tokens[3].start : stop], read=dialect, into=exp.Table)
    if backend == "gp":
        table = normalize_identifiers(table, dialect=dialect)
    return {
        "action": "show",
        "parsed": True,
        "objects": [
            {
                "kind": "catalog" if noun == "SCHEMAS" else "schema",
                "catalog": (table.db or None) if noun != "SCHEMAS" else None,
                "schema": None,
                "name": table.name,
            }
        ],
    }


def _objects(expression: exp.Expression, target: exp.Expression | None) -> list[dict[str, Any]]:
    cte_references = {
        id(node)
        for scope in traverse_scope(expression)
        for node, source in scope.selected_sources.values()
        if isinstance(source, Scope)
    }
    objects = []
    for table in expression.find_all(exp.Table):
        if id(table) in cte_references:
            continue
        kind = (
            str(expression.args.get("kind") or "table").lower()
            if table is target or isinstance(expression, exp.Drop)
            else "table"
        )
        reference = {
            "kind": kind,
            "catalog": table.catalog or None,
            "schema": table.db or None,
            "name": table.name or None,
        }
        if kind in {"schema", "database", "catalog"}:
            parts = [part.name for part in table.parts]
            reference.update(
                catalog=parts[-2] if len(parts) > 1 else None,
                schema=None,
                name=parts[-1] if parts else None,
            )
        if reference not in objects:
            objects.append(reference)
    return objects


def _targets(
    expression: exp.Expression, target: exp.Expression | None, objects: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    if isinstance(expression, (exp.Alter, exp.Drop)):
        return objects
    if isinstance(expression, exp.Create):
        return objects[:1] if isinstance(target, exp.Table) else []
    into = expression.find(exp.Into)
    if into is not None and isinstance(into.this, exp.Table):
        return [
            obj
            for obj in objects
            if obj["name"] == into.this.name and obj["schema"] == (into.this.db or None)
        ]
    return []


def _namespace(expression: exp.Expression, backend: str) -> dict[str, Any]:
    target = expression.args.get("this")
    if isinstance(expression, exp.Use) and isinstance(target, exp.Table):
        if backend == "trino":
            return {"schema": target.name, **({"catalog": target.db} if target.db else {})}
        return {"database": target.name}
    return {}


def _describe_expression(expression: exp.Expression, backend: str) -> dict[str, Any]:
    if backend == "gp":
        expression = normalize_identifiers(expression, dialect="postgres")
    target = expression.args.get("this")
    if isinstance(target, exp.Schema):
        target = target.this
    if isinstance(expression, exp.Alter) and isinstance(target, exp.Table):
        for rename in expression.find_all(exp.AlterRename):
            if isinstance(rename.this, exp.Table) and not rename.this.db:
                rename.this.set("db", target.args.get("db"))
                rename.this.set("catalog", target.args.get("catalog"))
    objects = _objects(expression, target)
    if isinstance(expression, exp.Use) and isinstance(target, exp.Table):
        objects = [
            {
                "kind": "schema" if backend == "trino" else "database",
                "catalog": target.db or None if backend == "trino" else None,
                "schema": None,
                "name": target.name,
            }
        ]
    return {
        "action": "select" if isinstance(expression, exp.Query) else expression.key,
        "parsed": True,
        "objects": objects,
        "targets": _targets(expression, target, objects),
        "namespace": _namespace(expression, backend),
    }


def describe_sql(sql: str, backend: str) -> list[dict[str, Any]]:
    descriptions = []
    for statement in split_statements(sql):
        item: dict[str, Any] = {"action": "unknown", "objects": [], "parsed": False}
        descriptions.append(item)
        try:
            namespace = namespace_command(statement, backend) or _show_namespace(statement, backend)
            if namespace is not None:
                item.update(namespace)
                continue
            parsed = sqlparse.parse(statement)
            item["action"] = parsed[0].get_type().lower() if parsed else "unknown"  # type: ignore[no-untyped-call]
            expression = sqlglot.parse_one(statement, read=_DIALECTS.get(backend, backend))
            if expression is not None and not isinstance(expression, exp.Command):
                item.update(_describe_expression(cast("exp.Expression", expression), backend))
        except Exception:  # noqa: BLE001 -- unsupported SQL must still execute and be saved.
            item["parsed"] = False
    return descriptions
