from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests._support.paths import REPO_ROOT
from tests.repository._support.source_validation import layout, load, product, result, transport
from tests.repository._support.workflow_scalars import expression, job_scalars


@pytest.mark.parametrize(
    ("event", "mode", "subject"),
    [
        ("push", False, ""),
        ("pull_request", False, ""),
        ("workflow_dispatch", False, ""),
        ("push", True, "a" * 40),
        ("workflow_dispatch", True, "a" * 40),
        ("workflow_dispatch", False, "a" * 40),
        ("workflow_dispatch", True, ""),
    ],
)
def test_source_mode_is_exclusive_and_cannot_mint_skipped_normal_fast_success(event, mode, subject):
    jobs = job_scalars(REPO_ROOT / ".github/workflows/tests.yml")
    selected = event == "workflow_dispatch" and (mode or bool(subject))
    arguments = {"event": event, "mode": mode, "subject": subject}
    assert bool(expression(jobs["source-validation"]["if"], **arguments)) is selected
    for job in ("fast-checks", "tests", "sdk-conformance", "quality"):
        assert bool(expression(jobs[job]["if"], **arguments)) is not selected
        name = expression(jobs[job]["name"], **arguments)
        if selected:
            assert name.startswith("Source-validation-only")
        elif job == "fast-checks":
            assert name == "fast-checks"
        elif job == "quality":
            assert name == "quality gates"


def test_workflow_condition_parser_refuses_unknown_execution_constructs():
    with pytest.raises(AssertionError, match="Unsupported"):
        expression("unknown()", event="push")


@pytest.mark.parametrize("valid", [False, True])
def test_real_guard_command_retains_metadata_without_subject_checkout_or_capture(
    tmp_path, monkeypatch, valid
):
    fixture = layout(tmp_path, monkeypatch)
    for key in ("VALIDATION_CONTROL_ROOT", "VALIDATION_TRANSPORT_ROOT", "VALIDATION_SUBJECT_ROOT"):
        fixture.env.pop(key)
    if not valid:
        fixture.env["GITHUB_TRIGGERING_ACTOR"] = "other"
    transport(fixture, monkeypatch)
    assert fixture.module.main(["validate"], fixture.env) is valid
    outputs, manifest = result(fixture)
    assert "metadata_root" in outputs
    assert "artifact_root" not in outputs
    assert not fixture.invocations
    assert manifest["guard_passed"] is valid
    assert manifest["step_outcome"] == ("success" if valid else "failure")
    assert manifest["stage"] == ("identity-validated" if valid else "identity")
    assert not manifest["ok"]
    assert not manifest["capture_attempted"]
    assert not manifest["visual_approval"]
    assert manifest["final_job_conclusion"] is None
    assert manifest["uploaded_artifacts"] is None


def test_workflow_retains_hidden_originals_and_early_guard_metadata():
    text = (REPO_ROOT / ".github/workflows/tests.yml").read_text()
    source = text.split("  source-validation:", 1)[1].split("  fast-checks:", 1)[0]
    guard, remaining = source.split("      - name: Checkout fixed reviewed transport", 1)
    assert "id: guard" in guard
    assert 'source_validation.py" validate' in guard
    metadata, capture = remaining.split("      - name: Retain complete or honestly partial", 1)
    assert "steps.guard.outputs.metadata_root != ''" in metadata
    assert "steps.capture.outputs.metadata_root || steps.guard.outputs.metadata_root" in metadata
    assert "include-hidden-files: true" in capture
    assert "steps.capture.outputs.artifact_root != ''" in capture


def test_complete_capture_uses_only_fixed_transport_and_retains_unapproved_evidence(
    tmp_path, monkeypatch
):
    fixture = layout(tmp_path, monkeypatch)
    fixture.env.update(
        GH_TOKEN="private-fixture",
        PYTHONPATH="untrusted-fixture",
        HTTPS_PROXY="proxy-fixture",
        GIT_CONFIG_VALUE_0="credential-fixture",
    )
    transport(fixture, monkeypatch)
    assert fixture.module.run(fixture.env)
    command, environment = fixture.invocations[0]
    assert command[1:] == ["-I", str(fixture.source)]
    assert set(environment) == {
        "PATH",
        "RUNNER_TEMP",
        "GITHUB_REPOSITORY",
        "GITHUB_RUN_ID",
        "GITHUB_RUN_ATTEMPT",
        "CANDIDATE",
        "CANDIDATE_ROOT",
        "GITHUB_OUTPUT",
    }
    assert Path(environment["GITHUB_OUTPUT"]) != Path(fixture.env["GITHUB_OUTPUT"])
    assert Path(environment["GITHUB_OUTPUT"]).stat().st_mode & 0o777 == 0o600
    outputs, manifest = result(fixture)
    assert Path(outputs["artifact_root"]).parent == fixture.roots["temporary"]
    assert manifest["role"] == "source-validation"
    assert manifest["ok"]
    assert len(manifest["capture_files"]) == 93
    assert manifest["expected_scene_count"] == 46
    assert manifest["final_job_conclusion"] is None
    assert manifest["uploaded_artifacts"] is None
    assert not manifest["visual_approval"]
    assert not manifest["production_activation"]
    assert manifest["individual_png_verdicts"] == "pending"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("SOURCE_VALIDATION", "false"),
        ("SOURCE_VALIDATION_SUBJECT", "branch"),
        ("GITHUB_ACTOR", "other"),
        ("GITHUB_TRIGGERING_ACTOR", "other"),
        ("GITHUB_EVENT_NAME", "pull_request"),
        ("GITHUB_REPOSITORY", "other/repo"),
        ("GITHUB_REF", "refs/heads/mutable"),
        ("GITHUB_WORKFLOW_SHA", "e" * 40),
        ("GITHUB_RUN_ID", "0"),
        ("GITHUB_RUN_ATTEMPT", "01"),
        ("VISUAL_ISOLATION_PROBE", "true"),
    ],
)
def test_bad_mode_actor_or_frozen_identity_fails_before_candidate_execution(
    tmp_path, monkeypatch, field, value
):
    fixture = layout(tmp_path, monkeypatch)
    fixture.env[field] = value
    transport(fixture, monkeypatch)
    assert not fixture.module.run(fixture.env)
    outputs, manifest = result(fixture)
    assert not fixture.invocations
    assert "artifact_root" not in outputs
    assert manifest["step_outcome"] == "failure"
    assert manifest["stage"] == "identity"


@pytest.mark.parametrize("change", ["source", "head", "overlap", "symlink", "manifest"])
def test_wrong_source_checkout_roots_or_scene_contract_never_executes_subject(
    tmp_path, monkeypatch, change
):
    fixture = layout(tmp_path, monkeypatch)
    if change == "source":
        fixture.source.write_text("changed transport")
    elif change == "head":
        fixture.heads[fixture.roots["subject"]] = "e" * 40
    elif change == "overlap":
        fixture.env["VALIDATION_SUBJECT_ROOT"] = str(fixture.roots["transport"])
    elif change == "symlink":
        fixture.source.rename(fixture.source.with_suffix(".original"))
        fixture.source.symlink_to(fixture.source.with_suffix(".original"))
    else:
        data = json.loads(fixture.manifest.read_text())
        data["scenes"].pop()
        fixture.manifest.write_text(json.dumps(data))
    transport(fixture, monkeypatch)
    assert not fixture.module.run(fixture.env)
    assert not fixture.invocations
    assert result(fixture)[1]["step_outcome"] == "failure"


@pytest.mark.parametrize(
    ("code", "interrupted", "output"), [(1, False, True), (0, True, True), (0, False, False)]
)
def test_capture_failure_or_suppressed_root_never_grants_success(
    tmp_path, monkeypatch, code, interrupted, output
):
    fixture = layout(tmp_path, monkeypatch)
    transport(fixture, monkeypatch, code=code, interrupted=interrupted, output=output)
    assert not fixture.module.run(fixture.env)
    outputs, manifest = result(fixture)
    assert manifest["transport_exit"] == code
    assert manifest["transport_interrupted"] == interrupted
    assert ("artifact_root" in outputs) is (output and not interrupted)
    assert not manifest["visual_approval"]
    assert manifest["final_job_conclusion"] is None


@pytest.mark.parametrize(
    "kind", ["duplicate", "unknown", "outside", "oversize", "symlink", "empty"]
)
def test_transport_output_is_admitted_as_bounded_owned_data(tmp_path, kind):
    module = load()
    temporary = tmp_path / "temporary"
    temporary.mkdir()
    root = temporary / ("atk-visual-" + "a" * 32)
    root.mkdir()
    command = tmp_path / "output"
    text = "artifact_root=" + str(root) + "\n"
    if kind == "duplicate":
        text += text
    elif kind == "unknown":
        text = "metadata_root=" + str(root) + "\n"
    elif kind == "outside":
        text = "artifact_root=" + str(tmp_path) + "\n"
    elif kind == "oversize":
        text += "x" * module.OUTPUT_LIMIT
    elif kind == "symlink":
        root.rmdir()
        root.symlink_to(tmp_path, target_is_directory=True)
    elif kind == "empty":
        text = ""
    command.write_text(text)
    if kind == "empty":
        assert module.admit_output(command, temporary) is None
    else:
        with pytest.raises((ValueError, OSError)):
            module.admit_output(command, temporary)


@pytest.mark.parametrize("kind", ["missing", "extra", "bytes", "geometry", "dimensions", "session"])
def test_incomplete_or_changed_product_evidence_cannot_pass(tmp_path, kind):
    module = load()
    names = [f"scene-{index}" for index in range(46)]
    artifact = product(tmp_path / "capture", names, "d" * 40)
    session_file = artifact / "capture.json"
    session = json.loads(session_file.read_text())
    if kind == "missing":
        (artifact / (names[0] + ".png")).unlink()
    elif kind == "extra":
        (artifact / "extra").write_text("unassigned")
    elif kind == "bytes":
        (artifact / (names[0] + ".png")).write_bytes(b"changed")
    elif kind in {"geometry", "dimensions"}:
        suffix = ".json" if kind == "geometry" else ".png"
        path = artifact / (names[0] + suffix)
        body = (
            json.dumps({"scene_id": names[0], "ok": False}).encode()
            if kind == "geometry"
            else path.read_bytes()[:-1] + b"\x00"
        )
        path.write_bytes(body)
        key = "geometry_sha256" if kind == "geometry" else "screenshot_sha256"
        session["scenes"][names[0]][key] = hashlib.sha256(body).hexdigest()
    else:
        session["candidate"] = "e" * 40
    session_file.write_text(json.dumps(session))
    with pytest.raises(ValueError, match="Source-validation"):
        module.capture_inventory(artifact.parent, names, "d" * 40)


def test_regular_files_reject_hardlinks_and_do_not_reject_normal_directory_link_counts(tmp_path):
    module = load()
    child = tmp_path / "child"
    child.mkdir()
    assert module.directory(tmp_path) == tmp_path
    source = child / "source"
    source.write_bytes(b"source")
    assert module.regular(source, 6) == b"source"
    os.link(source, child / "alias")
    with pytest.raises(ValueError, match="Source-validation file is unsafe"):
        module.regular(source, 6)


def test_git_identity_does_not_forward_credentials_or_enable_repository_hooks(
    tmp_path, monkeypatch
):
    module = load()
    (tmp_path / ".git").mkdir()
    calls = []

    def git(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout="a" * 40 + "\n")

    monkeypatch.setattr(module.subprocess, "run", git)
    assert module.git_head(tmp_path, {"PATH": "/trusted", "GH_TOKEN": "private"}) == "a" * 40
    command, options = calls[0]
    assert "core.hooksPath=/dev/null" in command
    assert "core.fsmonitor=false" in command
    assert "GH_TOKEN" not in options["env"]
    assert options["timeout"] == 30


@pytest.mark.parametrize("kill", [False, True])
def test_interrupted_transport_gets_cleanup_time_before_forced_exit(tmp_path, monkeypatch, kill):
    module = load()
    calls = []
    waits = [subprocess.TimeoutExpired("transport", 1)]
    if kill:
        waits.append(subprocess.TimeoutExpired("transport", 1))
    waits.append(-9 if kill else -15)

    class Process:
        pid = 123
        returncode = None

        def wait(self, timeout):
            calls.append(("wait", timeout))
            value = waits.pop(0)
            if isinstance(value, Exception):
                raise value
            self.returncode = value
            return value

    monkeypatch.setattr(module.subprocess, "Popen", lambda *args, **kwargs: Process())
    monkeypatch.setattr(module.signal, "signal", lambda *args: signal.SIG_DFL)
    monkeypatch.setattr(module.os, "killpg", lambda pid, value: calls.append(("signal", value)))
    code, interrupted = module.invoke(["trusted"], {})
    assert interrupted
    assert code == (-9 if kill else -15)
    assert ("signal", signal.SIGTERM) in calls
    assert ("wait", 120) in calls
    assert (("signal", signal.SIGKILL) in calls) is kill
