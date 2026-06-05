#!/usr/bin/env bash
set -euo pipefail

script_dir="$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(CDPATH='' cd -- "${script_dir}/.." && pwd)"

cd "${repo_root}"

python <<'PY'
from __future__ import annotations

import importlib
import inspect
import pathlib

MODULES = {
    "ab_utils": "analytics_toolkit.ab_utils",
    "dates": "analytics_toolkit.dates",
    "excel": "analytics_toolkit.excel",
    "sql": "analytics_toolkit.sql",
}

CANONICAL_DOC_NAMES = {
    "sql": {
        "execute_sql": "execute",
        "read_sql": "read",
        "transfer_table": "transfer",
    }
}


def doc_stem(module_key: str, name: str) -> str:
    canonical_name = CANONICAL_DOC_NAMES.get(module_key, {}).get(name, name)
    if module_key == "sql":
        return canonical_name
    return canonical_name.replace("_", "-")


def read_links(path: pathlib.Path) -> set[str]:
    text = path.read_text(encoding="utf-8")
    links: set[str] = set()
    marker = "]("
    start = 0
    while True:
        index = text.find(marker, start)
        if index == -1:
            return links
        link_start = index + len(marker)
        link_end = text.find(")", link_start)
        if link_end == -1:
            return links
        links.add(text[link_start:link_end])
        start = link_end + 1


failures: list[str] = []

for module_key, module_name in MODULES.items():
    module = importlib.import_module(module_name)
    docs_dir = pathlib.Path("docs/modules") / module_key / "functions"
    index_path = docs_dir / "index.md"
    if not index_path.is_file():
        failures.append(f"{index_path} is missing")
        continue

    index_links = read_links(index_path)

    for export_name in getattr(module, "__all__", []):
        exported = getattr(module, export_name)
        if not inspect.isfunction(exported):
            continue
        if not exported.__module__.startswith(f"{module_name}."):
            continue

        stem = doc_stem(module_key, export_name)
        page_name = f"{stem}.md"
        page_path = docs_dir / page_name
        if not page_path.is_file():
            failures.append(
                f"{module_name}.{export_name} is missing function docs page {page_path}"
            )
            continue
        if page_name not in index_links:
            failures.append(
                f"{module_name}.{export_name} docs page {page_name} is not linked from {index_path}"
            )

if failures:
    raise SystemExit("\n".join(failures))

print("Function documentation coverage is complete.")
PY
