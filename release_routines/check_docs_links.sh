#!/usr/bin/env bash
set -euo pipefail

script_dir="$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(CDPATH='' cd -- "${script_dir}/.." && pwd)"

cd "${repo_root}"

python <<'PY'
from __future__ import annotations

import pathlib
import re

ROOT_README = pathlib.Path("README.md")
DOCS_ROOT = pathlib.Path("docs")
MODULES_ROOT = DOCS_ROOT / "modules"


def read(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


def lines(path: pathlib.Path) -> list[str]:
    return [line.rstrip() for line in read(path).splitlines()]


def links(path: pathlib.Path) -> list[str]:
    return re.findall(r"\[[^\]]+\]\(([^)]+)\)", read(path))


def local_markdown_links(path: pathlib.Path) -> list[str]:
    return [
        link
        for link in links(path)
        if not link.startswith(("http://", "https://", "#", "mailto:"))
        and link.endswith(".md")
    ]


def has_link(path: pathlib.Path, target: str) -> bool:
    return target in links(path)


def starts_and_ends(path: pathlib.Path, expected_line: str) -> bool:
    content_lines = [line for line in lines(path) if line]
    return bool(content_lines) and content_lines[0] == expected_line and content_lines[-1] == expected_line


def assert_file_exists(source: pathlib.Path, link: str, failures: list[str]) -> None:
    target = (source.parent / link).resolve()
    try:
        target.relative_to(pathlib.Path.cwd().resolve())
    except ValueError:
        failures.append(f"{source} links outside repository: {link}")
        return
    if not target.is_file():
        failures.append(f"{source} links to missing file: {link}")


failures: list[str] = []

root_readme = read(ROOT_README)
if "docs/README.md" not in root_readme:
    failures.append("README.md must link to docs/README.md")

docs_readme = pathlib.Path("docs/README.md")
for required_link in [
    "../README.md",
    "QUICK_START.md",
    "AIRFLOW_SQL_MANUAL.md",
    "modules/README.md",
    "CHANGELOG.md",
]:
    if not has_link(docs_readme, required_link):
        failures.append(f"{docs_readme} must link to {required_link}")

for doc_path in sorted(DOCS_ROOT.glob("*.md")):
    if doc_path == docs_readme:
        continue
    if not starts_and_ends(doc_path, "[Documentation overview](README.md)"):
        failures.append(
            f"{doc_path} must start and end with [Documentation overview](README.md)"
        )

modules_readme = MODULES_ROOT / "README.md"
if not starts_and_ends(modules_readme, "[Documentation overview](../README.md)"):
    failures.append(
        f"{modules_readme} must start and end with [Documentation overview](../README.md)"
    )

module_dirs = sorted(
    directory for directory in MODULES_ROOT.iterdir() if directory.is_dir() and (directory / "index.md").is_file()
)
for module_dir in module_dirs:
    module_index_link = f"{module_dir.name}/index.md"
    if not has_link(modules_readme, module_index_link):
        failures.append(f"{modules_readme} must link to {module_index_link}")

for module_dir in module_dirs:
    module_index = module_dir / "index.md"
    if not starts_and_ends(module_index, "[All module docs](../README.md)"):
        failures.append(
            f"{module_index} must start and end with [All module docs](../README.md)"
        )

    module_text = read(module_index)
    function_heading = re.search(r"^## All .+ Functions$", module_text, flags=re.MULTILINE)
    workflow_heading = re.search(r"^## Workflow Guides$", module_text, flags=re.MULTILINE)
    if function_heading is None:
        failures.append(f"{module_index} must contain an All Functions section")
    if workflow_heading is None:
        failures.append(f"{module_index} must contain a Workflow Guides section")
    if function_heading is not None and workflow_heading is not None and function_heading.start() > workflow_heading.start():
        failures.append(f"{module_index} must list function reference before workflow guides")
    if not has_link(module_index, "functions/index.md"):
        failures.append(f"{module_index} must link to functions/index.md")

    for link in local_markdown_links(module_index):
        assert_file_exists(module_index, link, failures)

    function_index = module_dir / "functions" / "index.md"
    expected_function_index_links = {
        "ab_utils": "[AB utilities index](../index.md)",
        "dates": "[Date helpers index](../index.md)",
        "excel": "[Excel helpers index](../index.md)",
        "sql": "[SQL module index](../index.md)",
    }
    function_index_line = expected_function_index_links.get(module_dir.name, "[Module index](../index.md)")
    if not starts_and_ends(function_index, function_index_line):
        failures.append(f"{function_index} must start and end with {function_index_line}")

    for link in local_markdown_links(function_index):
        assert_file_exists(function_index, link, failures)

    function_page_links = {
        "ab_utils": "[All AB functions](index.md)",
        "dates": "[All date functions](index.md)",
        "excel": "[All Excel functions](index.md)",
        "sql": "[SQL functions index](index.md)",
    }
    function_page_line = function_page_links.get(module_dir.name, "[Functions index](index.md)")
    for function_page in sorted((module_dir / "functions").glob("*.md")):
        if function_page.name == "index.md":
            continue
        if not starts_and_ends(function_page, function_page_line):
            failures.append(
                f"{function_page} must start and end with a link back to functions/index.md"
            )

    workflow_links = [
        link
        for link in local_markdown_links(module_index)
        if not link.startswith(("functions/", "../"))
    ]
    module_page_links = {
        "ab_utils": "[AB utilities index](index.md)",
        "dates": "[Date helpers index](index.md)",
        "excel": "[Excel helpers index](index.md)",
        "sql": "[SQL module index](index.md)",
    }
    module_page_line = module_page_links.get(module_dir.name, "[Module index](index.md)")
    for link in workflow_links:
        workflow_path = module_index.parent / link
        if workflow_path.name == "index.md":
            continue
        if not starts_and_ends(workflow_path, module_page_line):
            failures.append(
                f"{workflow_path} must start and end with a link back to its module index"
            )

for doc_path in sorted(MODULES_ROOT.rglob("*.md")):
    for link in local_markdown_links(doc_path):
        assert_file_exists(doc_path, link, failures)

if failures:
    raise SystemExit("\n".join(dict.fromkeys(failures)))

print("Documentation links are consistent.")
PY
