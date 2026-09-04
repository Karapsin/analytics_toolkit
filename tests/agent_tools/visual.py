from __future__ import annotations

import asyncio
import json
import re
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest
from textual.app import ScreenStackError

from agent_tools import mcp_server, sql_explorer_visual, sql_explorer_visual_scene
from tests.agent_tools._support.mcp import _init_git_repo, _write_minimal_repo_files

if TYPE_CHECKING:
    from pathlib import Path


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
    session["capture"] = {"status": "pass", "vm_name": "fresh-vm", "vm_deleted": True}
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
    assert len(manifest["scenes"]) == 15
    assert manifest["viewport"] == {
        "platform": "macos",
        "width": 1280,
        "height": 800,
        "terminal_profile": "SQL Explorer Visual Review",
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
        else:
            application = sql_explorer_visual_scene.VisualExplorerApp(
                scene_id,
                evidence,
                manifest,
            )
        async with application.run_test(size=(160, 47)) as pilot:
            for _attempt in range(100):
                await pilot.pause(0.1)
                if evidence.is_file():
                    geometry = json.loads(evidence.read_text(encoding="utf-8"))
                    if geometry["ok"] is True:
                        break

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


def test_capture_refuses_any_preexisting_vm(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _write_minimal_repo_files(tmp_path / "project")
    _write_visual_repo(root)
    _init_git_repo(root)
    session = sql_explorer_visual.start_review(root, "review-collision")
    fingerprint = session["content_fingerprint"]
    monkeypatch.setattr(sql_explorer_visual.shutil, "which", lambda _name: "/opt/homebrew/bin/tart")
    monkeypatch.setattr(sql_explorer_visual, "content_fingerprint", lambda _root: fingerprint)
    monkeypatch.setattr(
        sql_explorer_visual,
        "_run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    with pytest.raises(sql_explorer_visual.VisualReviewError, match="pre-existing"):
        sql_explorer_visual.capture_review(root, session["review_id"])


def test_guest_environment_includes_eager_sql_imports(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[str] = []
    monkeypatch.setattr(
        sql_explorer_visual,
        "_guest_shell",
        lambda _root, _vm_name, script, **_kwargs: captured.append(script),
    )

    sql_explorer_visual._prepare_guest(
        mcp_server.REPO_ROOT,
        "fresh-vm",
        "/Volumes/My Shared Files/review/checkout",
        "review-runtime",
    )

    assert len(captured) == 1
    assert "tqdm>=4.65,<5" in captured[0]


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


def test_terminal_scene_keeps_textual_output_attached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[str] = []
    monkeypatch.setattr(
        sql_explorer_visual,
        "_guest_shell",
        lambda _root, _vm_name, script, **_kwargs: captured.append(script),
    )

    sql_explorer_visual._open_terminal_scene(
        mcp_server.REPO_ROOT,
        "fresh-vm",
        scene_id="editor-ready",
        guest_root="/Volumes/My Shared Files/review/checkout",
        guest_output="/Volumes/My Shared Files/review/output",
        venv="/tmp/review-venv",
    )

    assert len(captured) == 1
    assert "open -na Terminal" in captured[0]
    assert "2>" not in captured[0]
    assert "\\033[3;0;24t\\033[8;61;208t" in captured[0]
    assert "TEXTUAL_COLOR_SYSTEM=256" in captured[0]
    assert "SQL_EXPLORER_VISUAL_REQUIRE_COLOR_256=1" in captured[0]
    assert "unset COLORTERM" in captured[0]


def test_guest_visual_profile_is_opaque_and_hides_the_dock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[str] = []
    monkeypatch.setattr(
        sql_explorer_visual,
        "_guest_shell",
        lambda _root, _vm_name, script, **_kwargs: captured.append(script),
    )

    sql_explorer_visual._configure_guest_ui(mcp_server.REPO_ROOT, "fresh-vm")

    assert len(captured) == 1
    assert "Window Settings.Pro.BackgroundColor" in captured[0]
    assert sql_explorer_visual.OPAQUE_BLACK_NS_COLOR in captured[0]
    assert "-string Pro" in captured[0]
    assert "com.apple.dock autohide -bool true" in captured[0]


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
