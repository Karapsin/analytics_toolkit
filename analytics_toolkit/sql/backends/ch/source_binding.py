"""Retain table bindings when native table sources become cluster functions."""

from __future__ import annotations

from collections import Counter
from typing import cast

from sqlglot import exp
from sqlglot.optimizer.scope import Scope, traverse_scope


def _bound_source(
    scope: Scope,
    column: exp.Column,
    bindings: dict[int, list[exp.Table]],
    default_database: str | None = None,
) -> exp.Table | None:
    bound = _bound_reference(scope, column, bindings, default_database)
    return bound[0] if bound is not None else None


def _bound_reference(
    scope: Scope,
    column: exp.Column,
    bindings: dict[int, list[exp.Table]],
    default_database: str | None,
) -> tuple[exp.Table, int] | None:
    owner: Scope | None = scope
    while owner is not None:
        candidates = [
            (source, prefix)
            for source in bindings.get(id(owner), [])
            if (prefix := _source_prefix_length(column, source, default_database))
        ]
        if candidates:
            # A bare name shared by several local sources is ambiguous. Do not
            # guess one or fall through to an unrelated enclosing source.
            longest = max(prefix for _source, prefix in candidates)
            matches = [item for item in candidates if item[1] == longest]
            return matches[0] if len(matches) == 1 else None
        # A CTE/subquery/other source with the same alias shadows ancestors.
        if column.parts[0].name in owner.sources:
            return None
        owner = owner.parent
    return None


def _source_prefix_length(
    column: exp.Column, source: exp.Table, default_database: str | None
) -> int:
    parts = [part.name for part in column.parts]
    prefixes = [[source.alias_or_name]]
    database = source.db or default_database
    if database:
        prefixes.append([database, source.name])
        if source.catalog:
            prefixes.append([source.catalog, database, source.name])
    return max(
        (
            len(prefix)
            for prefix in prefixes
            if len(parts) > len(prefix) and parts[: len(prefix)] == prefix
        ),
        default=0,
    )


def _matches_source(
    column: exp.Column, source: exp.Table, default_database: str | None = None
) -> bool:
    return bool(_source_prefix_length(column, source, default_database))


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


def _local_scope_columns(scope: Scope) -> list[exp.Column]:
    columns = {
        id(column): column
        for reference in [*scope.columns, *scope.stars]
        for column in reference.find_all(exp.Column)
        if column.table and column.find_ancestor(exp.Query) is scope.expression
    }
    return list(columns.values())


def preserve_source_bindings(
    statement: exp.Expression, default_database: str | None = None
) -> None:
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
        for column in _local_scope_columns(scope):
            bound = _bound_reference(scope, column, bindings, default_database)
            if bound is None:
                continue
            bound_source, prefix = bound
            fields = column.parts[prefix:]
            replacement: exp.Expression = exp.Column(
                this=fields[0].copy(), table=aliases[id(bound_source)].copy()
            )
            for field in fields[1:]:
                replacement = exp.Dot(this=replacement, expression=field.copy())
            replacement.add_comments(column.comments)
            column.replace(replacement)
            qualified_sources.add(id(bound_source))
    for sources in bindings.values():
        for table in sources:
            if not table.alias and id(table) in qualified_sources:
                table.set("alias", exp.TableAlias(this=aliases[id(table)].copy()))
