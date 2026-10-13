"""Static audit of scheduling delays and direct source-spelling inspection."""

from __future__ import annotations

import ast


def _import_aliases(tree: ast.AST) -> dict[str, str]:
    aliases = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                aliases[alias.asname or alias.name.split(".")[0]] = alias.name
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                # Some existing domain support re-exports stdlib modules.
                name = (
                    alias.name
                    if alias.name in {"time", "asyncio", "inspect"}
                    else (f"{node.module}.{alias.name}")
                )
                aliases[alias.asname or alias.name] = name
    return aliases


def _dotted(node: ast.AST, aliases: dict[str, str]) -> str:
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        return f"{_dotted(node.value, aliases)}.{node.attr}"
    return ""


class _SensitiveCalls(ast.NodeVisitor):
    def __init__(self, aliases: dict[str, str]) -> None:
        self.aliases = aliases
        self.owner = "<module>"
        self.found: list[tuple[str, str, str]] = []

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        previous = self.owner
        if node.name.startswith("test_"):
            self.owner = node.name
        self.generic_visit(node)
        self.owner = previous

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.visit_FunctionDef(node)

    def visit_Call(self, node: ast.Call) -> None:
        name = _dotted(node.func, self.aliases)
        if name in {"time.sleep", "asyncio.sleep", "inspect.getsource"}:
            # ast.unparse is unavailable on the supported Python 3.8 floor.
            argument = (
                repr(node.args[0].value)
                if node.args and isinstance(node.args[0], ast.Constant)
                else "expression"
            )
            self.found.append((self.owner, name, argument))
        self.generic_visit(node)


def sensitive_calls(source: str) -> list[tuple[str, str, str]]:
    tree = ast.parse(source)
    visitor = _SensitiveCalls(_import_aliases(tree))
    visitor.visit(tree)
    return visitor.found
