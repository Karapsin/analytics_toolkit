from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
from typing import Any

import pytest

from agent_tools import sql_explorer_visual as visual
from tests._support.paths import REPO_ROOT

spec = importlib.util.spec_from_file_location(
    "capture_artifact", REPO_ROOT / ".github/agent/capture_artifact.py"
)
assert spec is not None
assert spec.loader is not None
artifact = importlib.util.module_from_spec(spec)
spec.loader.exec_module(artifact)
IDENTITY = {"candidate": "a" * 40, "repository": "owner/repo", "run_id": 17, "run_attempt": 2}


def _failed_capture(root: Path) -> Path:
    evidence = root / visual.STATE_ROOT / "review-1"
    (evidence / "geometry").mkdir(parents=True)
    (evidence / "process/command-completion").mkdir(parents=True)
    session_file = visual.session_path(root, "review-1")
    session_file.parent.mkdir(parents=True)
    session_file.write_text(
        json.dumps(
            {
                "capture": {"status": "failed", "workspace_removed": True},
                "scenes": {
                    "command-completion": {"capture": "pending", "geometry": ""},
                    "next-scene": {"capture": "pending"},
                },
            }
        )
    )
    (evidence / "geometry/command-completion.json").write_text(
        json.dumps({"ok": False, "checks": [{"name": "scroll", "pass": False}]})
    )
    (evidence / "process/command-completion/stdout.bin").write_bytes(
        ("output λ\n" * 20000).encode()
    )
    (evidence / "process/command-completion/stderr.bin").write_bytes(b"actual failure\n")
    return evidence


def test_failed_scene_pending_paths_and_complete_large_output_survive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence = _failed_capture(tmp_path)

    def workflow(action: str, **_kwargs: Any) -> dict[str, Any]:
        return {"ok": action == "start", "review_id": "review-1", "error": "scene failed"}

    monkeypatch.setattr(visual, "visual_workflow", workflow)
    assert artifact.capture_artifact(tmp_path, IDENTITY) is False
    destination = tmp_path / "visual-diagnostics"
    manifest = json.loads((destination / "diagnostics.json").read_bytes())
    assert manifest["collection_complete"] is True
    assert manifest["visual_approval"] is False
    assert manifest["capture_result"]["ok"] is False
    assert not (tmp_path / "visual-artifact").exists()
    assert len(manifest["files"]) == 4
    for name, record in manifest["files"].items():
        source = (
            visual.session_path(tmp_path, "review-1")
            if name == "raw/session.json"
            else evidence / name[len("raw/evidence/") :]
        )
        raw = (destination / name).read_bytes()
        assert raw == source.read_bytes()
        assert len(raw) == record["bytes"]
        assert hashlib.sha256(raw).hexdigest() == record["sha256"]
    assert (
        destination / "raw/evidence/process/command-completion/stdout.bin"
    ).stat().st_size > 65536


def test_successful_capture_keeps_legacy_flat_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence = tmp_path / visual.STATE_ROOT / "review-1"
    (evidence / "screenshots").mkdir(parents=True)
    (evidence / "geometry").mkdir()
    png = evidence / "screenshots/editor-ready.png"
    geometry = evidence / "geometry/editor-ready.json"
    png.write_bytes(b"inert png fixture")
    geometry.write_text('{"ok": true}')
    session = {
        "capture": {"status": "pass"},
        "scenes": {"editor-ready": {"screenshot": str(png), "geometry": str(geometry)}},
    }
    session_file = visual.session_path(tmp_path, "review-1")
    session_file.parent.mkdir(parents=True)
    session_file.write_text(json.dumps(session))
    monkeypatch.setattr(
        visual, "visual_workflow", lambda *_a, **_k: {"ok": True, "review_id": "review-1"}
    )
    assert artifact.capture_artifact(tmp_path, IDENTITY) is True
    assert (tmp_path / "visual-artifact/editor-ready.png").read_bytes() == png.read_bytes()
    assert (
        json.loads((tmp_path / "visual-artifact/capture.json").read_bytes())["candidate"]
        == "a" * 40
    )
    assert not (tmp_path / "visual-diagnostics").exists()


def test_start_failure_is_explicitly_incomplete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        visual, "visual_workflow", lambda *_a, **_k: {"ok": False, "error": "start"}
    )
    assert artifact.capture_artifact(tmp_path, IDENTITY) is False
    manifest = json.loads((tmp_path / "visual-diagnostics/diagnostics.json").read_bytes())
    assert manifest["collection_complete"] is False
    assert manifest["files"] == {}


@pytest.mark.parametrize("unsafe", ["symlink", "hardlink", "file-size", "total-size", "file-count"])
def test_unsafe_or_oversized_export_fails_without_complete_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unsafe: str,
) -> None:
    evidence = _failed_capture(tmp_path)
    if unsafe == "symlink":
        (evidence / "link").symlink_to(evidence / "geometry/command-completion.json")
    elif unsafe == "hardlink":
        os.link(evidence / "geometry/command-completion.json", evidence / "link")
    elif unsafe == "file-size":
        monkeypatch.setattr(artifact, "MAX_FILE", 10)
    elif unsafe == "total-size":
        monkeypatch.setattr(artifact, "MAX_TOTAL", 10)
    else:
        monkeypatch.setattr(artifact, "MAX_FILES", 1)
    with pytest.raises(ValueError, match=r"[Dd]iagnostic|capacity|bounds"):
        artifact.export_failure(tmp_path / "out", evidence, identity=IDENTITY, result={"ok": False})
    assert not (tmp_path / "out/diagnostics.json").exists()


def test_failure_upload_remains_separate_and_always_runs() -> None:
    # Exact trusted workflow policy, as in the repository workflow provenance tests.
    workflow = (REPO_ROOT / ".github/workflows/agent-visual.yml").read_text()
    assert "if: always() && steps.capture.outcome == 'failure'" in workflow
    assert "if-no-files-found: error" in workflow
    assert "${{ github.run_id }}-${{ github.run_attempt }}" in workflow
    assert "ref: ${{ github.sha }}" in workflow
    assert "run: python .agent-visual-controller/.github/agent/capture_artifact.py" in workflow
    assert "continue-on-error" not in workflow


@pytest.mark.parametrize("replacement", ["symlink", "fifo", "parent", "root"])
def test_descriptor_read_rejects_replacement_without_following_or_blocking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement: str,
) -> None:
    boundary = tmp_path / "raw"
    folder = boundary / "nested"
    folder.mkdir(parents=True)
    target = folder / "victim.bin"
    target.write_bytes(b"safe")
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"leak")
    real_open = artifact.os.open
    replaced = False

    def opening(name: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        nonlocal replaced
        if name == "victim.bin" and not replaced:
            replaced = True
            if replacement == "symlink":
                target.unlink()
                target.symlink_to(outside)
            elif replacement == "fifo":
                target.unlink()
                os.mkfifo(target)
            elif replacement == "parent":
                folder.rename(boundary / "old")
                folder.mkdir()
                target.write_bytes(b"leak")
            else:
                boundary.rename(tmp_path / "old-raw")
                folder.mkdir(parents=True)
                target.write_bytes(b"leak")
        return real_open(name, flags, *args, **kwargs)

    monkeypatch.setattr(artifact.os, "open", opening)
    with pytest.raises((OSError, ValueError), match=r"Diagnostic|symbolic|levels|[Ll]oop"):
        artifact._regular(target, boundary)
    assert replaced is True


@pytest.mark.parametrize("replacement", ["parent", "leaf"])
def test_descriptor_publication_never_writes_through_replaced_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement: str,
) -> None:
    destination = tmp_path / "artifact"
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "data.bin"
    victim.write_bytes(b"original")
    real_open = artifact.os.open
    replaced = False

    def opening(name: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        nonlocal replaced
        if name == "data.bin" and flags & os.O_WRONLY and not replaced:
            replaced = True
            if replacement == "parent":
                (destination / "raw").rename(destination / "old-raw")
                (destination / "raw").symlink_to(outside, target_is_directory=True)
            else:
                (destination / "raw/data.bin").symlink_to(victim)
        return real_open(name, flags, *args, **kwargs)

    monkeypatch.setattr(artifact.os, "open", opening)
    with pytest.raises((OSError, ValueError), match=r"Artifact|symbolic|levels|exists|directory"):
        artifact._publish(destination, [("raw/data.bin", b"changed")])
    assert victim.read_bytes() == b"original"
    assert replaced is True


def test_pinned_legacy_capture_retains_available_geometry_with_unknown_stream_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise unchanged dev capture failure, which has no per-child stream files."""
    review = "review-legacy"
    evidence = tmp_path / visual.STATE_ROOT / review
    session_file = visual.session_path(tmp_path, review)
    session_file.parent.mkdir(parents=True)
    session_file.write_text(
        json.dumps(
            {
                "review_id": review,
                "content_fingerprint": "pinned-dev-content",
                "capture_mode": visual.CAPTURE_MODE,
                "capture": {"status": "pending"},
                "scenes": {"command-completion": {"capture": "pending", "geometry": ""}},
            }
        )
    )
    monkeypatch.setattr(visual, "content_fingerprint", lambda _root: "pinned-dev-content")
    monkeypatch.setattr(visual, "_copy_review_tree", lambda _root, _checkout: None)
    monkeypatch.setattr(visual, "start_review", lambda *_args, **_kwargs: {"review_id": review})
    geometry = json.dumps(
        {
            "scene_id": "command-completion",
            "ok": False,
            "checks": [{"name": "command overlay geometry", "pass": False}],
            "export_input_evidence": {"original": "界λ" * 40000},
        },
        ensure_ascii=False,
    ).encode()

    def failed_child(command: list[str], **_kwargs: Any) -> None:
        target = Path(command[command.index("--evidence") + 1])
        target.write_bytes(geometry)
        message = "original legacy child assertion"
        raise visual.VisualReviewError(message)

    monkeypatch.setattr(visual, "_run", failed_child)
    assert artifact.capture_artifact(tmp_path, IDENTITY) is False
    output = tmp_path / "visual-diagnostics"
    manifest = json.loads((output / "diagnostics.json").read_bytes())
    assert manifest["collection_complete"] is True
    assert manifest["process_output_complete"] is None
    assert manifest["visual_approval"] is False
    assert set(manifest["files"]) == {
        "raw/session.json",
        "raw/evidence/geometry/command-completion.json",
    }
    assert (output / "raw/evidence/geometry/command-completion.json").read_bytes() == geometry
    session = json.loads((output / "raw/session.json").read_bytes())
    assert session["capture"]["status"] == "failed"
    assert session["capture"]["workspace_removed"] is True
    assert session["capture"]["error"] == "original legacy child assertion"
    assert session["scenes"]["command-completion"]["capture"] == "pending"
    assert not (evidence / "process").exists()
    assert not (tmp_path / "visual-artifact").exists()
