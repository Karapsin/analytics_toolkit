"""Dependency-aware test selection for environments that omit lagging extras.

While the rolling Python support window is extended, an extra may not install
on the newest CPython. The compatibility matrix then sets ``ATK_WITHOUT_EXTRAS``
(for example ``tui``) and the root conftest ignores every test module that
imports, directly or through ``tests`` support modules, a dependency of that
extra. Extras whose tests already skip without their SDK need no entry.
"""

from __future__ import annotations

import ast
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

ENV_VAR = "ATK_WITHOUT_EXTRAS"
EXTRA_IMPORTS = {
    "tui": (
        "textual",
        "rich",
        "tree_sitter",
        "tree_sitter_languages",
        "tree_sitter_sql",
        "pyperclip",
        "analytics_toolkit.sql_explorer",
        "agent_tools.sql_explorer",
    ),
}


def omitted_extras(environ: dict[str, str] | None = None) -> set[str]:
    value = (os.environ if environ is None else environ).get(ENV_VAR, "")
    return {item.strip() for item in value.split(",") if item.strip()}


def _module_name(tests_root: Path, path: Path) -> str:
    parts = list(path.relative_to(tests_root.parent).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _imports(path: Path, module: str) -> set[str]:
    package = module if path.name == "__init__.py" else module.rpartition(".")[0]
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                anchor = package.split(".")[: len(package.split(".")) - node.level + 1]
                base = ".".join([*anchor, base] if base else anchor)
            names.add(base)
            names.update(f"{base}.{alias.name}" for alias in node.names)
    return names


def _matches(name: str, prefixes: tuple[str, ...]) -> bool:
    return any(name == prefix or name.startswith(prefix + ".") for prefix in prefixes)


def dependent_paths(tests_root: Path, extras: set[str]) -> list[Path]:
    """Test files that transitively import a dependency of any omitted extra."""
    prefixes = tuple(prefix for extra in sorted(extras) for prefix in EXTRA_IMPORTS.get(extra, ()))
    if not prefixes:
        return []
    files = {
        _module_name(tests_root, path): path
        for path in tests_root.rglob("*.py")
        if "__pycache__" not in path.parts
    }
    imports = {module: _imports(path, module) for module, path in files.items()}
    dependent = {
        module for module, names in imports.items() if any(_matches(n, prefixes) for n in names)
    }
    changed = True
    while changed:
        changed = False
        for module, names in imports.items():
            if module not in dependent and names & dependent:
                dependent.add(module)
                changed = True
    return sorted(files[module] for module in dependent)


def collect_ignore(tests_root: Path, environ: dict[str, str] | None = None) -> list[str]:
    """Paths relative to ``tests_root`` for the root conftest ``collect_ignore``."""
    ignored = []
    for path in dependent_paths(tests_root, omitted_extras(environ)):
        target = path.parent if path.name in {"conftest.py", "__init__.py"} else path
        ignored.append(str(target.relative_to(tests_root)))
    return sorted(set(ignored))
