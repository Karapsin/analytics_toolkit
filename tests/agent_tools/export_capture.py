from __future__ import annotations

import asyncio
import json
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from rich.console import Console

from agent_tools import sql_explorer_visual_capture as capture


def _app(exports: list[str], *, error: Exception | None = None) -> Any:
    def export(**kwargs: object) -> str:
        exports.append(str(kwargs["title"]))
        if error is not None:
            raise error
        console = Console(file=StringIO(), record=True, width=20)
        console.print("SQL")
        return console.export_svg(unique_id="captured")

    return SimpleNamespace(export_screenshot=export)


def test_success_keeps_geometry_private_until_final_atomic_publication(tmp_path: Path) -> None:
    geometry: dict[str, Any] = {"ok": True}
    path = tmp_path / "scene.json"
    path.write_text('{"ok": true}', encoding="utf-8")
    calls: list[str] = []
    original = Console.export_svg
    svg = capture._observed_screenshot(_app(calls), "completion-narrowed", geometry, path)
    assert svg.startswith("<svg") or "<?xml" in svg
    assert calls == ["SQL Explorer — completion-narrowed"]
    assert Console.export_svg is original
    assert json.loads(path.read_text()) == {"ok": True}
    assert "export_observation" in geometry
    assert len(geometry["export_observation"]["invocations"]) == 1
    assert not path.with_suffix(".tmp").exists()


def test_export_failure_retains_original_exception_and_incomplete_observation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "scene.json"
    geometry: dict[str, Any] = {"ok": True}
    error = RuntimeError("producer failed")
    calls: list[str] = []
    original = Console.export_svg
    with pytest.raises(RuntimeError) as caught:
        capture._observed_screenshot(
            _app(calls, error=error), "connections-searching", geometry, path
        )
    assert caught.value is error
    assert Console.export_svg is original
    assert calls == ["SQL Explorer — connections-searching"]
    retained = json.loads(path.read_text(encoding="utf-8"))
    assert not retained["export_observation"]["complete"]
    assert retained["export_observation"]["missing"] == ["expected_one_recording_export"]
    assert not path.with_suffix(".tmp").exists()


def test_mapping_failure_does_not_change_exported_svg(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def mapping_unavailable(*_args: object) -> None:
        message = "unsupported mapping"
        raise ValueError(message)

    monkeypatch.setattr(capture, "map_export_spans", mapping_unavailable)
    calls: list[str] = []
    geometry: dict[str, Any] = {"ok": True}
    svg = capture._observed_screenshot(_app(calls), "completion-narrowed", geometry, tmp_path / "g")
    assert svg
    assert len(calls) == 1
    assert not geometry["export_observation"]["complete"]
    assert "svg_mapping:ValueError" in geometry["export_observation"]["missing"]


def test_io_failure_does_not_mask_cancelled_export(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    def cannot_write(*_args: object, **_kwargs: object) -> None:
        message = "read-only artifact"
        raise OSError(message)

    monkeypatch.setattr(Path, "write_text", cannot_write)
    error = asyncio.CancelledError()

    def cancelled(**_kwargs: object) -> str:
        raise error

    with pytest.raises(asyncio.CancelledError) as caught:
        capture._observed_screenshot(
            SimpleNamespace(export_screenshot=cancelled), "completion-narrowed", {}, tmp_path / "g"
        )
    assert caught.value is error
    assert "Could not retain failed export observation" in caplog.text
