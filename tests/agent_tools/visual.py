from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from xml.etree import ElementTree as ET

import pytest
from textual.app import ScreenStackError

from agent_tools import (
    mcp_server,
    sql_explorer_visual,
    sql_explorer_visual_capture,
    sql_explorer_visual_scene,
)
from tests.agent_tools._support.mcp import _init_git_repo, _write_minimal_repo_files


def _write_visual_repo(root: Path) -> None:
    explorer = root / "analytics_toolkit/sql_explorer"
    explorer.mkdir(parents=True)
    (explorer / "app.py").write_text('Widget(id="query-editor")\n', encoding="utf-8")
    manifest = root / sql_explorer_visual.MANIFEST
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "review_batch_size": 5,
                "scenes": [{"id": "editor-ready", "required_elements": ["#query-editor"]}],
            }
        ),
        encoding="utf-8",
    )


def _complete_fake_capture(root: Path, review_id: str) -> None:
    session = sql_explorer_visual._load_session(root, review_id)
    evidence = root / ".rag_index/evidence"
    evidence.mkdir(parents=True)
    screenshot = evidence / "editor-ready.png"
    geometry = evidence / "editor-ready.json"
    screenshot.write_bytes(b"png")
    geometry.write_text("{}", encoding="utf-8")
    session["capture"] = {"status": "pass", "workspace_removed": True}
    session["scenes"]["editor-ready"].update(
        {
            "capture": "pass",
            "screenshot": str(screenshot),
            "screenshot_sha256": sql_explorer_visual._sha256(screenshot),
            "geometry": str(geometry),
            "geometry_sha256": sql_explorer_visual._sha256(geometry),
        }
    )
    sql_explorer_visual._write_json(
        sql_explorer_visual.session_path(root, review_id),
        session,
    )


def test_visual_manifest_covers_every_literal_sql_explorer_element() -> None:
    root = mcp_server.REPO_ROOT
    required: set[str] = set()
    for path in (root / "analytics_toolkit/sql_explorer").glob("*.py"):
        required.update(
            f"#{element}"
            for element in re.findall(r'id="([A-Za-z0-9_-]+)"', path.read_text(encoding="utf-8"))
        )
    manifest = sql_explorer_visual._manifest(root)
    covered = {element for scene in manifest["scenes"] for element in scene["required_elements"]}

    assert required <= covered
    assert {"tab-database-change", "formatted-sql", "save-changes-cancel"} <= {
        scene["id"] for scene in manifest["scenes"]
    }
    assert manifest["viewport"] == {
        "platform": "current-host",
        "width": 1280,
        "height": 800,
        "renderer": "textual-headless",
        "columns": 208,
        "rows": 47,
    }


@pytest.mark.parametrize(
    "scene_id",
    [scene["id"] for scene in sql_explorer_visual._manifest(mcp_server.REPO_ROOT)["scenes"]],
)
def test_visual_scene_publishes_complete_geometry(scene_id: str, tmp_path: Path) -> None:
    evidence = tmp_path / f"{scene_id}.json"
    manifest = mcp_server.REPO_ROOT / sql_explorer_visual.MANIFEST

    async def exercise() -> None:
        if scene_id == "database-picker":
            application = sql_explorer_visual_scene.VisualDatabasePickerApp(evidence, manifest)
        elif scene_id in {
            "connections-picker",
            "connections-searching",
            "connections-empty",
            "connections-error",
        }:
            application = sql_explorer_visual_scene.VisualConnectionsPickerApp(
                scene_id, evidence, manifest
            )
        else:
            application = sql_explorer_visual_scene.VisualExplorerApp(
                scene_id,
                evidence,
                manifest,
            )
        async with application.run_test(size=(208, 47)) as pilot:
            if isinstance(application, sql_explorer_visual_scene.VisualConnectionsPickerApp):
                assert len(application.screen_stack) == 2
                assert len({id(screen) for screen in application.screen_stack}) == 2
            for _attempt in range(100):
                await pilot.pause(0.1)
                if evidence.is_file():
                    geometry = json.loads(evidence.read_text(encoding="utf-8"))
                    if geometry["ok"] is True:
                        break
            # Geometry can arrive before queued editor-change messages. Drain those
            # messages while their workspace is still mounted, before test teardown.
            await pilot.pause()

    asyncio.run(exercise())
    geometry = json.loads(evidence.read_text(encoding="utf-8"))
    assert geometry["ok"] is True, geometry["assertions"]


def test_visual_evidence_refresh_ignores_screen_teardown(tmp_path: Path) -> None:
    class ClosingApp:
        screen_stack = (object(),)

        @property
        def screen(self) -> None:
            message = "No screens on stack"
            raise ScreenStackError(message)

    refreshed = sql_explorer_visual_scene._refresh_evidence_if_mounted(
        cast("Any", ClosingApp()),
        "editor-ready",
        tmp_path / "evidence.json",
        mcp_server.REPO_ROOT / sql_explorer_visual.MANIFEST,
    )

    assert refreshed is False
    assert not (tmp_path / "evidence.json").exists()


def test_overflow_tab_restore_ignores_screen_teardown() -> None:
    class ClosingOverflowApp:
        visual_scene_id = "tabs-overflow"
        active_workspace = SimpleNamespace(tab_id="9")

        def _activate_tab(self, _tab_id: str) -> None:
            message = "workspace-9.find_bar"
            raise sql_explorer_visual_scene.NoMatches(message)

    restored = sql_explorer_visual_scene.VisualExplorerApp._restore_primary_overflow_tab(
        cast("Any", ClosingOverflowApp()),
    )

    assert restored is False


def test_visual_receipt_tracks_full_content_and_becomes_stale(tmp_path: Path) -> None:
    root = _write_minimal_repo_files(tmp_path / "project")
    _write_visual_repo(root)
    _init_git_repo(root)
    (root / "analytics_toolkit/sql_explorer/app.py").write_text(
        'Widget(id="query-editor")\n# polished\n',
        encoding="utf-8",
    )

    session = sql_explorer_visual.start_review(root, "review-12345678")
    _complete_fake_capture(root, session["review_id"])
    sql_explorer_visual.record_review(root, session["review_id"], "editor-ready", "pass")
    complete = sql_explorer_visual.complete_review(root, session["review_id"])

    assert complete["ok"] is True
    assert (
        sql_explorer_visual.verify_visual_receipt(
            root,
            paths=["analytics_toolkit/sql_explorer/app.py"],
        )["ok"]
        is True
    )

    (root / "analytics_toolkit/sql_explorer/app.py").write_text(
        'Widget(id="query-editor")\n# changed after review\n',
        encoding="utf-8",
    )
    stale = sql_explorer_visual.verify_visual_receipt(
        root,
        paths=["analytics_toolkit/sql_explorer/app.py"],
    )
    assert stale["ok"] is False
    assert "stale" in stale["message"]


def test_non_pass_review_requires_notes_and_blocks_completion(tmp_path: Path) -> None:
    root = _write_minimal_repo_files(tmp_path / "project")
    _write_visual_repo(root)
    _init_git_repo(root)
    session = sql_explorer_visual.start_review(root, "review-87654321")
    _complete_fake_capture(root, session["review_id"])

    with pytest.raises(sql_explorer_visual.VisualReviewError, match="requires notes"):
        sql_explorer_visual.record_review(
            root,
            session["review_id"],
            "editor-ready",
            "product_defect",
        )
    sql_explorer_visual.record_review(
        root,
        session["review_id"],
        "editor-ready",
        "product_defect",
        "tab is clipped",
    )
    with pytest.raises(sql_explorer_visual.VisualReviewError, match="non-pass"):
        sql_explorer_visual.complete_review(root, session["review_id"])


def test_geometry_waits_for_the_scene_final_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    geometry = tmp_path / "completion.json"
    geometry.write_text('{"ok": false}\n', encoding="utf-8")
    monkeypatch.setattr(
        sql_explorer_visual.time,
        "sleep",
        lambda _seconds: geometry.write_text('{"ok": true}\n', encoding="utf-8"),
    )

    assert sql_explorer_visual._wait_geometry(geometry, timeout=1)["ok"] is True


@pytest.mark.parametrize("screen", [{"width": 208, "height": 52}, {"width": 220, "height": 47}])
def test_geometry_rejects_terminal_grid_that_would_be_clipped(
    tmp_path: Path, screen: dict[str, int]
) -> None:
    geometry = tmp_path / "completion.json"
    geometry.write_text(json.dumps({"ok": True, "screen": screen}), encoding="utf-8")
    with pytest.raises(sql_explorer_visual.VisualReviewError, match="capture viewport"):
        sql_explorer_visual._wait_geometry(geometry)


def test_git_workflow_blocks_visual_change_without_current_receipt(tmp_path: Path) -> None:
    root = _write_minimal_repo_files(tmp_path / "project")
    _write_visual_repo(root)
    _init_git_repo(root)
    (root / "analytics_toolkit/sql_explorer/app.py").write_text(
        'Widget(id="query-editor")\n# visual change\n',
        encoding="utf-8",
    )

    result = mcp_server.git_workflow(
        "commit",
        message="Polish Explorer",
        paths=["analytics_toolkit/sql_explorer/app.py"],
        root=str(root),
    )

    assert result["ok"] is False
    assert result["blockers"][0]["phase"] == "visual_review"


def test_visual_cli_parsers_route_review_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        mcp_server.sql_explorer_visual,
        "visual_review",
        lambda **kwargs: captured.update(kwargs) or {"ok": True},
    )
    parser = mcp_server._build_cli_parser()
    args = parser.parse_args(
        [
            "visual-review",
            "--review-id",
            "review-12345678",
            "--scene-id",
            "editor-ready",
            "--verdict",
            "pass",
        ]
    )

    assert args.handler(args) == {"ok": True}
    assert captured["scene_id"] == "editor-ready"
    assert captured["verdict"] == "pass"


@pytest.mark.parametrize(
    ("host_os", "folder", "executable"),
    [("posix", "bin", "python"), ("nt", "Scripts", "python.exe")],
)
def test_host_interpreter_selection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, host_os: str, folder: str, executable: str
) -> None:
    interpreter = tmp_path / ".venv" / folder / executable
    interpreter.parent.mkdir(parents=True)
    interpreter.touch()
    with monkeypatch.context() as patch:
        patch.setattr(sql_explorer_visual.os, "name", host_os)
        selected = sql_explorer_visual._host_python(tmp_path)
    assert selected == str(interpreter)
    interpreter.unlink()
    assert sql_explorer_visual._host_python(tmp_path) == sql_explorer_visual.sys.executable


@pytest.mark.parametrize("fail", [False, True])
def test_host_capture_isolated_process_and_cleanup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fail: bool
) -> None:
    root = _write_minimal_repo_files(tmp_path / "project")
    _write_visual_repo(root)
    _init_git_repo(root)
    (root / ".connections").write_text("{}", encoding="utf-8")
    session = sql_explorer_visual.start_review(root, "review-host-capture")
    workspaces = []
    commands = []
    original_run = sql_explorer_visual._run

    def run(command: list[str], *, cwd: Path, **_kwargs: Any) -> Any:
        if command[0] == "git":
            return original_run(command, cwd=cwd, **_kwargs)
        commands.append(command)
        workspaces.append(cwd.parent)
        assert cwd != root
        assert (cwd / sql_explorer_visual.MANIFEST).is_file()
        assert not (cwd / ".connections").exists()
        if fail:
            message = "scene failed"
            raise sql_explorer_visual.VisualReviewError(message)
        evidence = command[command.index("--evidence") + 1]
        screenshot = command[command.index("--screenshot") + 1]
        Path(evidence).write_text(
            json.dumps(
                {"ok": True, "scene_id": "editor-ready", "screen": {"width": 208, "height": 47}}
            )
        )
        Path(screenshot).write_bytes(b"fake-png")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(sql_explorer_visual, "_run", run)
    monkeypatch.setattr(sql_explorer_visual, "_validate_png", lambda _path: None)
    if fail:
        with pytest.raises(sql_explorer_visual.VisualReviewError, match="scene failed"):
            sql_explorer_visual.capture_review(root, session["review_id"])
    else:
        status = sql_explorer_visual.capture_review(root, session["review_id"])
        assert status["pending_review_count"] == 1
    assert commands[0][1:3] == ["-m", "agent_tools.sql_explorer_visual_capture"]
    assert all(not path.exists() for path in workspaces)
    saved = sql_explorer_visual._load_session(root, session["review_id"])
    assert saved["capture"]["workspace_removed"] is True
    assert saved["capture"]["status"] == ("failed" if fail else "pass")
    with pytest.raises(sql_explorer_visual.VisualReviewError, match="already attempted"):
        sql_explorer_visual.capture_review(root, session["review_id"])


def test_headless_capture_exports_real_application_frame(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rendered = []
    monkeypatch.setattr(
        sql_explorer_visual_capture, "_write_png", lambda svg, path: rendered.append((svg, path))
    )
    evidence = tmp_path / "geometry.json"
    png = tmp_path / "scene.png"
    asyncio.run(
        sql_explorer_visual_capture.capture_scene(
            "editor-ready", evidence, mcp_server.REPO_ROOT / sql_explorer_visual.MANIFEST, png
        )
    )
    assert json.loads(evidence.read_text())["ok"] is True
    assert len(rendered) == 1
    assert "SELECT" in "".join(ET.fromstring(rendered[0][0]).itertext())
    assert rendered[0][1] == png


@pytest.mark.parametrize("view_box", ["0 0 2556 1196.8", "0 0 600 1200"])
def test_headless_frame_keeps_fixed_viewport_without_cropping(view_box: str) -> None:
    svg = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{view_box}"><text>SQL</text></svg>'
    frame = ET.fromstring(sql_explorer_visual_capture._frame_svg(svg))
    assert (frame.attrib["width"], frame.attrib["height"]) == ("1280", "800")
    screen = list(frame)[1]
    assert screen.attrib["viewBox"] == view_box
    assert screen.attrib["preserveAspectRatio"] == "xMidYMid meet"
    assert "SQL" in "".join(screen.itertext())
