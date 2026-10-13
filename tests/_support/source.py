"""Python architecture checks that ignore comments, quotes and layout."""

from __future__ import annotations

import ast
import inspect
from dataclasses import dataclass
from importlib.util import resolve_name
from textwrap import dedent
from typing import TYPE_CHECKING

from tests._support.paths import REPO_ROOT

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


def function_tree(function: Callable[..., object]) -> ast.Module:
    return ast.parse(dedent(inspect.getsource(function)))


def contains_expression(tree: ast.AST, expression: str) -> bool:
    expected = ast.dump(ast.parse(expression, mode="eval").body, include_attributes=False)
    return any(ast.dump(node, include_attributes=False) == expected for node in ast.walk(tree))


def called_names(tree: ast.AST) -> set[str]:
    return {
        node.func.id if isinstance(node.func, ast.Name) else node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, (ast.Name, ast.Attribute))
    }


def branches_on(tree: ast.AST, name: str) -> bool:
    return any(
        isinstance(child, ast.Name) and child.id == name
        for node in ast.walk(tree)
        if isinstance(node, ast.If)
        for child in ast.walk(node.test)
    )


def backend_branch_count(tree: ast.AST) -> int:
    """Count dispatch comparisons, including reversed and multiline operands."""
    count = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        operands = [node.left, *node.comparators]
        backend_reference = any(
            (isinstance(item, ast.Name) and (item.id == "backend" or item.id.endswith("_backend")))
            or (isinstance(item, ast.Attribute) and item.attr in {"backend", "connection_backend"})
            for item in operands
        )
        backend_literal = any(
            isinstance(child, ast.Constant) and child.value in ("gp", "trino", "ch")
            for item in operands
            for child in ast.walk(item)
        )
        if backend_reference and backend_literal:
            count += 1
    return count


def referenced_names(tree: ast.AST) -> set[str]:
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    names.update(node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute))
    names.update(
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    )
    names.update(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    )
    return names


def imported_modules(path: Path, *, package: str | None = None) -> set[str]:
    """Resolve relative imports against their real package, including aliases."""
    if package is None:
        package = ".".join(path.relative_to(REPO_ROOT).parent.parts)
    modules: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = "." * node.level + (node.module or "")
            base = resolve_name(module, package) if node.level else module
            modules.add(base)
            modules.update(f"{base}.{alias.name}" for alias in node.names)
    return modules


@dataclass(frozen=True)
class CodeRules:
    names: tuple[str, ...] = ()
    prefixes: tuple[str, ...] = ()
    expressions: tuple[str, ...] = ()
    calls_with: tuple[tuple[str, str], ...] = ()
    modules: tuple[str, ...] = ()
    strings: tuple[str, ...] = ()


def forbidden_code(path: Path, rules: CodeRules) -> list[str]:
    """Check explicit ownership rules against Python syntax and SQL literals."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    references = referenced_names(tree)
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    literals = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]
    imports = imported_modules(path)
    violations = set(rules.names) & references
    violations.update(
        prefix for prefix in rules.prefixes if any(name.startswith(prefix) for name in references)
    )
    violations.update(
        expression for expression in rules.expressions if contains_expression(tree, expression)
    )
    for method, argument in rules.calls_with:
        if any(
            isinstance(node, ast.Call)
            and (
                (isinstance(node.func, ast.Name) and node.func.id == method)
                or (isinstance(node.func, ast.Attribute) and node.func.attr == method)
            )
            and any(isinstance(value, ast.Name) and value.id == argument for value in node.args)
            for node in ast.walk(tree)
        ):
            violations.add(f"{method}({argument})")
    violations.update(
        module
        for module in rules.modules
        if any(imported == module or imported.startswith(module + ".") for imported in imports)
    )
    violations.update(
        fragment for fragment in rules.strings if any(fragment in value for value in literals)
    )
    return sorted(violations)
