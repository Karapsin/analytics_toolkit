"""Cross-domain helpers must remain isolated and dependency-light."""

from __future__ import annotations

import subprocess
import sys
from typing import TYPE_CHECKING

from tests._support.clock import FakeClock
from tests._support.files import read_json, write_json
from tests._support.paths import REPO_ROOT

if TYPE_CHECKING:
    from pathlib import Path


def test_json_fixture_roundtrip_creates_nested_utf8_files_without_shared_state(
    tmp_path: Path,
) -> None:
    path = write_json(tmp_path / "recipe" / "settings.json", {"title": "Привет", "items": [1]})
    first = read_json(path)
    first["items"].append(2)
    assert read_json(path) == {"title": "Привет", "items": [1]}


def test_fake_clock_advances_without_sleeping_and_keeps_instances_isolated() -> None:
    first = FakeClock()
    second = FakeClock(10)
    first.sleep(2.5)
    assert first() == first.monotonic() == first.now == 2.5
    first.set(7)
    assert first.monotonic() == 7
    assert second() == 10


def test_common_support_imports_do_not_load_optional_sdks() -> None:
    code = """
import sys
sys.modules['textual'] = None
sys.modules['datalens_sdk'] = None
import tests._support.clock
import tests._support.files
assert 'analytics_toolkit.sql_explorer' not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
