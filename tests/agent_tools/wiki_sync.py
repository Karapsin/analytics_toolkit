from __future__ import annotations

import json
import subprocess
import sys
from typing import TYPE_CHECKING

import pytest

from agent_tools.wiki_sync import MANIFEST, anchors, export_pages, sync_pages
from tests._support.paths import REPO_ROOT

if TYPE_CHECKING:
    from pathlib import Path


def make_docs(root: Path) -> Path:
    source = root / "docs"
    (source / "modules/sql/functions").mkdir(parents=True)
    files = {
        "README.md": (
            "# Documentation\n\n- [Modules](modules/README.md)\n"
            "- [Tests](INTEGRATION_TESTING.md)\n\n## Agent workflows\n\n"
            "- [Agent](MISSING.md)\n"
        ),
        "QUICK_START.md": "# Quick start\n\n[Read](modules/sql/functions/read.md#usage)\n",
        "AIRFLOW_SQL_MANUAL.md": "# Airflow\n",
        "CHANGELOG.md": "# Changelog\n",
        "INTEGRATION_TESTING.md": "# Tests\n",
        "modules/README.md": "# Modules\n\n- [SQL index](sql/index.md)\n",
        "modules/sql/index.md": "# SQL\n\n[Functions](functions/index.md)\n",
        "modules/sql/functions/index.md": "# Functions\n\n[read](read.md)\n",
        "modules/sql/functions/read.md": "# read\n\n## Usage\n\n## Usage\n",
    }
    for name, value in files.items():
        (source / name).write_text(value, encoding="utf-8")
    (root / "README.md").write_text("# Project\n", encoding="utf-8")
    return source


def test_repository_export_and_sidebar() -> None:
    pages = export_pages(REPO_ROOT / "docs")
    assert len(pages) > 100
    assert "sql-functions-read.md" in pages
    assert "INTEGRATION_TESTING.md" not in pages
    assert "WIKI_SYNC.md" not in pages
    assert "Agent workflows" not in pages["Home.md"]
    assert "INTEGRATION_TESTING" not in pages["Home.md"]
    assert "[Function reference](sql-functions-index)" in pages["_Sidebar.md"]
    assert pages == export_pages(REPO_ROOT / "docs")


def test_links_references_images_anchors_and_code_are_preserved(tmp_path: Path) -> None:
    source = make_docs(tmp_path)
    (tmp_path / "image.png").write_bytes(b"image")
    examples = """\n[Function](modules/sql/functions/read.md#usage-1 "title")
[`read`](<modules/sql/functions/read.md#usage>)
[Guide][guide]
[guide]: modules/sql/functions/read.md#usage "title"
![Image](../image.png)
[Project](../README.md)
[Tests](INTEGRATION_TESTING.md)
[External](https://example.com/path)
[Email](mailto:example@example.com)
[Local](#quick-start)
`[code](missing.md)`
``[long code](missing.md)``
    [indented](missing.md)
```python
[example](missing.md)
```
~~~text
[example](missing.md)
~~~
"""
    (source / "QUICK_START.md").write_text("# Quick start\n" + examples, encoding="utf-8")
    page = export_pages(source)["QUICK_START.md"]
    assert '/wiki/sql-functions-read#usage-1 "title")' in page
    assert "<https://github.com/Karapsin/analytics_toolkit/wiki/sql-functions-read#usage>" in page
    assert (
        "[guide]: https://github.com/Karapsin/analytics_toolkit/wiki/sql-functions-read#usage"
        in page
    )
    assert "/raw/main/image.png" in page
    assert "/blob/main/README.md" in page
    assert "/blob/main/docs/INTEGRATION_TESTING.md" in page
    assert "https://example.com/path" in page
    assert "/wiki/QUICK_START#quick-start" in page
    assert examples[examples.index("`[code]") :] in page


@pytest.mark.parametrize(
    "link", ["missing.md", "modules/sql/functions/read.md#absent", "../../outside.md"]
)
def test_invalid_links_leave_existing_output_unchanged(tmp_path: Path, link: str) -> None:
    source = make_docs(tmp_path)
    output = tmp_path / "wiki"
    sync_pages(output, export_pages(source))
    before = {path.name: path.read_bytes() for path in output.iterdir()}
    (source / "QUICK_START.md").write_text(f"# Quick start\n[Broken]({link})\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"Missing|escapes"):
        sync_pages(output, export_pages(source))
    assert before == {path.name: path.read_bytes() for path in output.iterdir()}


def test_colliding_flat_names_fail(tmp_path: Path) -> None:
    source = make_docs(tmp_path)
    (source / "modules/sql-functions-read.md").write_text("# Collision\n", encoding="utf-8")
    with pytest.raises(ValueError, match="colliding"):
        export_pages(source)


def test_sync_is_idempotent_removes_owned_pages_preserves_manual_pages(tmp_path: Path) -> None:
    output = tmp_path / "wiki"
    output.mkdir()
    (output / "Home.md").write_text("initial home", encoding="utf-8")
    (output / "Manual.md").write_text("manual page", encoding="utf-8")
    assert sync_pages(output, {"Home.md": "generated", "Old.md": "old"})
    before = {p.name: p.stat().st_mtime_ns for p in output.iterdir()}
    assert not sync_pages(output, {"Home.md": "generated", "Old.md": "old"})
    assert before == {p.name: p.stat().st_mtime_ns for p in output.iterdir()}
    assert sync_pages(output, {"Home.md": "generated", "New.md": "new"})
    assert not (output / "Old.md").exists()
    assert (output / "Manual.md").read_text(encoding="utf-8") == "manual page"
    assert json.loads((output / MANIFEST).read_text(encoding="utf-8")) == ["Home.md", "New.md"]


def test_unowned_collision_is_checked_before_deleting_anything(tmp_path: Path) -> None:
    sync_pages(tmp_path, {"Old.md": "owned"})
    (tmp_path / "Manual.md").write_text("manual", encoding="utf-8")
    with pytest.raises(ValueError, match="unowned"):
        sync_pages(tmp_path, {"Manual.md": "replacement"})
    assert (tmp_path / "Old.md").exists()
    assert (tmp_path / "Manual.md").read_text(encoding="utf-8") == "manual"


@pytest.mark.parametrize("value", [{}, ["../escape.md"], ["a/b.md"], [42]])
def test_invalid_manifest_cannot_delete_files(tmp_path: Path, value: object) -> None:
    (tmp_path / MANIFEST).write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError, match="manifest"):
        sync_pages(tmp_path, {"Home.md": "home"})
    assert not (tmp_path / "Home.md").exists()


def test_symlinks_are_rejected(tmp_path: Path) -> None:
    source = make_docs(tmp_path)
    (source / "modules/link.md").symlink_to(source / "QUICK_START.md")
    with pytest.raises(ValueError, match="symlink"):
        export_pages(source)
    output = tmp_path / "wiki"
    output.mkdir()
    (output / "Home.md").symlink_to(source / "QUICK_START.md")
    with pytest.raises(ValueError, match="symlink"):
        sync_pages(output, {"Home.md": "home"})


def test_cli_check_only_creates_no_output(tmp_path: Path) -> None:
    source = make_docs(tmp_path)
    output = tmp_path / "wiki"
    script = REPO_ROOT / "agent_tools/wiki_sync.py"
    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--source-directory",
            str(source),
            "--output-directory",
            str(output),
            "--check-only",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "Validated" in result.stdout
    assert not output.exists()


def test_heading_ids_preserve_duplicates_and_unicode() -> None:
    assert anchors("# Hello `world`!\n## Hello world!\n## Привет мир\n````\n## Hidden\n````\n") == {
        "hello-world",
        "hello-world-1",
        "привет-мир",
    }


def test_manifest_symlink_cannot_mutate_external_file(tmp_path: Path) -> None:
    output = tmp_path / "wiki"
    output.mkdir()
    external = tmp_path / "manifest.json"
    external.write_text("[]", encoding="utf-8")
    (output / MANIFEST).symlink_to(external)
    with pytest.raises(ValueError, match="manifest symlinks"):
        sync_pages(output, {"Home.md": "generated"})
    assert external.read_text(encoding="utf-8") == "[]"
    assert not (output / "Home.md").exists()


def test_output_directory_collision_cannot_delete_owned_pages(tmp_path: Path) -> None:
    sync_pages(tmp_path, {"Old.md": "owned"})
    (tmp_path / "Home.md").mkdir()
    with pytest.raises(ValueError, match="not a file"):
        sync_pages(tmp_path, {"Home.md": "generated"})
    assert (tmp_path / "Old.md").exists()


def test_cli_rejects_output_inside_source(tmp_path: Path) -> None:
    source = make_docs(tmp_path)
    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "agent_tools/wiki_sync.py"),
            "--source-directory",
            str(source),
            "--output-directory",
            str(source / "wiki"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "separate from source" in result.stderr
    assert not (source / "wiki").exists()
