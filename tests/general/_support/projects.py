"""A script directory separate from runtime cwd for path-resolution tests."""

from __future__ import annotations

from typing import TYPE_CHECKING

from analytics_toolkit.general.read_file import read_file

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def script_query_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, query: str) -> Path:
    runtime = tmp_path / "runtime"
    script = tmp_path / "dag_task"
    queries = script / "sql"
    runtime.mkdir()
    queries.mkdir(parents=True)
    (queries / "query.sql").write_text(query, encoding="utf-8")
    monkeypatch.chdir(runtime)
    monkeypatch.setitem(read_file.__globals__, "_resolve_base_dir", lambda: script)
    return script
