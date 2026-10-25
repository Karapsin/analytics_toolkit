"""Offline boundaries for native source-validation lifecycle regressions."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace

from tests._support.paths import REPO_ROOT


def load():
    spec = importlib.util.spec_from_file_location(
        "native_source_validation", REPO_ROOT / ".github/agent/source_validation.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def layout(tmp_path, monkeypatch):
    module = load()
    roots = {name: tmp_path / name for name in ("control", "transport", "subject", "temporary")}
    for name, path in roots.items():
        path.mkdir()
        if name != "temporary":
            (path / ".git").mkdir()
    workflow = "c" * 40
    candidate = "d" * 40
    heads = {
        roots["control"]: workflow,
        roots["transport"]: module.TRANSPORT,
        roots["subject"]: candidate,
    }

    def git(command, **kwargs):
        return SimpleNamespace(
            returncode=0, stdout=heads[Path(command[command.index("-C") + 1])] + "\n"
        )

    monkeypatch.setattr(module.subprocess, "run", git)
    source = roots["transport"] / ".github/agent/capture_isolation.py"
    source.parent.mkdir(parents=True)
    source.write_text("trusted synthetic fixture\n")
    monkeypatch.setattr(
        module,
        "SOURCES",
        {".github/agent/capture_isolation.py": hashlib.sha256(source.read_bytes()).hexdigest()},
    )
    names = [f"scene-{index:02d}" for index in range(46)]
    manifest = roots["subject"] / "visual-tests/sql_explorer/scenes.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "viewport": {"columns": 208, "rows": 47, "width": 1280, "height": 800},
                "scenes": [{"id": name} for name in names],
            }
        )
    )
    environment = {
        "PATH": os.environ["PATH"],
        "SOURCE_VALIDATION": "true",
        "SOURCE_VALIDATION_SUBJECT": candidate,
        "GITHUB_SHA": workflow,
        "GITHUB_WORKFLOW_SHA": workflow,
        "GITHUB_REPOSITORY": module.REPOSITORY,
        "GITHUB_ACTOR": "Karapsin",
        "GITHUB_TRIGGERING_ACTOR": "Karapsin",
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_RUN_ID": "123",
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_REF": "refs/tags/agent-source-validation/v1/" + workflow,
        "GITHUB_OUTPUT": str(tmp_path / "actions-output"),
        "RUNNER_TEMP": str(roots["temporary"]),
        "VALIDATION_CONTROL_ROOT": str(roots["control"]),
        "VALIDATION_TRANSPORT_ROOT": str(roots["transport"]),
        "VALIDATION_SUBJECT_ROOT": str(roots["subject"]),
    }
    return SimpleNamespace(
        module=module,
        roots=roots,
        env=environment,
        names=names,
        heads=heads,
        manifest=manifest,
        source=source,
        invocations=[],
    )


def product(folder, names, candidate):
    folder.mkdir()
    artifact = folder / "visual-artifact"
    artifact.mkdir()
    scenes = {}
    for name in names:
        # Header-only synthetic data tests shape validation; it is never a visual verdict.
        png = (
            b"\x89PNG\r\n\x1a\n"
            b"\x00\x00\x00\rIHDR" + (1280).to_bytes(4, "big") + (800).to_bytes(4, "big")
        )
        geometry = json.dumps({"scene_id": name, "ok": True}).encode()
        (artifact / (name + ".png")).write_bytes(png)
        (artifact / (name + ".json")).write_bytes(geometry)
        scenes[name] = {
            "capture": "pass",
            "verdict": "pending",
            "screenshot_sha256": hashlib.sha256(png).hexdigest(),
            "geometry_sha256": hashlib.sha256(geometry).hexdigest(),
        }
    (artifact / "capture.json").write_text(
        json.dumps(
            {
                "candidate": candidate,
                "capture": {"status": "pass", "workspace_removed": True},
                "scenes": scenes,
            }
        )
    )
    return artifact


def transport(fixture, monkeypatch, *, code=0, interrupted=False, output=True):
    def invoke(command, environment):
        fixture.invocations.append((command, environment))
        root = fixture.roots["temporary"] / ("atk-visual-" + "a" * 32)
        product(root, fixture.names, fixture.env["SOURCE_VALIDATION_SUBJECT"])
        if output:
            Path(environment["GITHUB_OUTPUT"]).write_text("artifact_root=" + str(root) + "\n")
        return code, interrupted

    monkeypatch.setattr(fixture.module, "invoke", invoke)


def result(fixture):
    outputs = dict(
        line.split("=", 1) for line in Path(fixture.env["GITHUB_OUTPUT"]).read_text().splitlines()
    )
    manifest = json.loads((Path(outputs["metadata_root"]) / "source-validation.json").read_text())
    return outputs, manifest
