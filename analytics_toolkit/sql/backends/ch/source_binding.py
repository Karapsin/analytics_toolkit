"""Retain table bindings when native table sources become cluster functions."""

from __future__ import annotations

from collections import Counter
from typing import cast

from sqlglot import exp
from sqlglot.optimizer.scope import Scope, traverse_scope


def _bound_source(
    scope: Scope, column: exp.Column, bindings: dict[int, list[exp.Table]]
) -> exp.Table | None:
    owner: Scope | None = scope
    while owner is not None:
        candidates = [
            source for source in bindings.get(id(owner), []) if _matches_source(column, source)
        ]
        if candidates:
            # A bare name shared by several local sources is ambiguous. Do not
            # guess one or fall through to an unrelated enclosing source.
            return candidates[0] if len(candidates) == 1 else None
        # A CTE/subquery/other source with the same alias shadows ancestors.
        if column.table in owner.sources and not column.db and not column.catalog:
            return None
        owner = owner.parent
    return None


def _matches_source(column: exp.Column, source: exp.Table) -> bool:
    if column.db or column.catalog:
        return (
            column.table == source.name
            and (not column.db or column.db == source.db)
            and (not column.catalog or column.catalog == source.catalog)
        )
    return column.table == source.alias_or_name


def _source_alias(source: exp.Table, names: Counter[str], ordinal: int) -> exp.Identifier:
    alias = source.args.get("alias")
    if isinstance(alias, exp.TableAlias) and isinstance(alias.this, exp.Identifier):
        return alias.this
    identifier = cast("exp.Identifier", source.this)
    if names[source.name] > 1:
        while f"_atk_source_{ordinal}" in names:
            ordinal += 1
        identifier = exp.to_identifier(f"_atk_source_{ordinal}")
        names[identifier.name] += 1
    return identifier


def preserve_source_bindings(statement: exp.Expression) -> None:
    scopes = list(traverse_scope(statement) or [])
    bindings: dict[int, list[exp.Table]] = {}
    qualified_sources: set[int] = set()
    names = Counter(name for scope in scopes for name, _source in scope.references)
    aliases: dict[int, exp.Identifier] = {}
    for scope in scopes:
        bindings[id(scope)] = [
            source
            for _name, source in scope.references
            if isinstance(source, exp.Table)
            and isinstance(source.this, exp.Identifier)
            and (source.db or source.name not in scope.cte_sources)
        ]
        for source in bindings[id(scope)]:
            aliases[id(source)] = _source_alias(source, names, len(aliases) + 1)
    for scope in scopes:
        columns = [
            column
            for reference in [*scope.columns, *scope.stars]
            for column in reference.find_all(exp.Column)
        ]
        for column in columns:
            if not column.table:
                continue
            bound_source = _bound_source(scope, column, bindings)
            if bound_source is None:
                continue
            column.set("table", aliases[id(bound_source)].copy())
            column.set("db", None)
            column.set("catalog", None)
            qualified_sources.add(id(bound_source))
    for sources in bindings.values():
        for table in sources:
            if not table.alias and id(table) in qualified_sources:
                table.set("alias", exp.TableAlias(this=aliases[id(table)].copy()))
