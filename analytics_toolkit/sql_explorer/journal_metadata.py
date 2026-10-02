"""Best-effort SQL action and object metadata for journal records."""

from __future__ import annotations

from typing import Any

import sqlglot
import sqlparse
from sqlglot import exp
from sqlglot.optimizer.scope import Scope, traverse_scope

from analytics_toolkit._sql_statements import split_statements

_DIALECTS = {"gp": "postgres", "trino": "trino", "ch": "clickhouse"}


def describe_sql(sql: str, backend: str) -> list[dict[str, Any]]:
    descriptions = []
    for statement in split_statements(sql):
        item: dict[str, Any] = {"action": "unknown", "objects": [], "parsed": False}
        descriptions.append(item)
        try:
            parsed = sqlparse.parse(statement)
            item["action"] = parsed[0].get_type().lower() if parsed else "unknown"  # type: ignore[no-untyped-call]
            expression = sqlglot.parse_one(statement, read=_DIALECTS.get(backend, backend))
            if expression is None or isinstance(expression, exp.Command):
                continue
            item["action"] = "select" if isinstance(expression, exp.Query) else expression.key
            item["parsed"] = True
            cte_references = {
                id(node)
                for scope in traverse_scope(expression)
                for node, source in scope.selected_sources.values()
                if isinstance(source, Scope)
            }
            objects = []
            target = expression.args.get("this")
            if isinstance(target, exp.Schema):
                target = target.this
            for table in expression.find_all(exp.Table):
                if id(table) in cte_references:
                    continue
                kind = (
                    str(expression.args.get("kind") or "table").lower()
                    if table is target
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
                        catalog=(parts[-2] if len(parts) > 1 else None),
                        schema=None,
                        name=parts[-1] if parts else None,
                    )
                if reference not in objects:
                    objects.append(reference)
            item["objects"] = objects
        except Exception:  # noqa: BLE001 -- unsupported SQL must still execute and be saved.
            item["parsed"] = False
    return descriptions
