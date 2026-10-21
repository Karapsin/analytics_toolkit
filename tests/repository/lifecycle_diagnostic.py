"""Fixed lifecycle diagnostics cannot become ordinary CI or production authority."""

from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests._support.paths import REPO_ROOT
from tests.repository._support.workflow_scalars import expression, job_scalars
from tests.repository.github_agent import controller

diagnostic = controller.trusted_module("lifecycle_diagnostic")
artifact = controller.trusted_module("capture_artifact")
containment = controller.trusted_module("capture_isolation")


def environment():
    source = "a" * 40
    return {
        "PATH": os.environ["PATH"],
        "LIFECYCLE_DIAGNOSTIC": "true",
        "DIAGNOSTIC_PYTHON": "3.11",
        "GITHUB_REPOSITORY": diagnostic.REPOSITORY,
        "GITHUB_SHA": source,
        "GITHUB_WORKFLOW_SHA": source,
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_ACTOR": "Karapsin",
        "GITHUB_TRIGGERING_ACTOR": "Karapsin",
        "GITHUB_RUN_ID": "123",
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_REF": "refs/tags/agent-source-validation/v1/" + source,
    }


@pytest.mark.parametrize("line", ["3.11", "3.13"])
def test_fixed_guard_profiles(line):
    env = environment()
    env["DIAGNOSTIC_PYTHON"] = line
    binding = diagnostic.identity(env)
    assert binding["candidate"] == diagnostic.SUBJECT
    assert binding["python_line"] == line
    assert binding["run_id"] == 123


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("LIFECYCLE_DIAGNOSTIC", "false"),
        ("LIFECYCLE_DIAGNOSTIC", "True"),
        ("GITHUB_REPOSITORY", "other/repo"),
        ("GITHUB_ACTOR", "other"),
        ("GITHUB_TRIGGERING_ACTOR", "other"),
        ("GITHUB_EVENT_NAME", "push"),
        ("GITHUB_SHA", "b" * 40),
        ("GITHUB_WORKFLOW_SHA", "b" * 40),
        ("GITHUB_REF", "refs/heads/dev"),
        ("DIAGNOSTIC_PYTHON", "3.12"),
        ("GITHUB_RUN_ID", "0"),
        ("GITHUB_RUN_ID", "True"),
        ("GITHUB_RUN_ATTEMPT", "-1"),
        ("GITHUB_RUN_ATTEMPT", "01"),
        ("SOURCE_VALIDATION", "true"),
        ("SOURCE_VALIDATION", "unexpected"),
        ("SOURCE_VALIDATION_SUBJECT", "b" * 40),
        ("VISUAL_ISOLATION_PROBE", "true"),
        ("GITHUB_WORKFLOW_REF", "other/.github/workflows/tests.yml@refs/heads/dev"),
    ],
)
def test_guard_refuses_before_source_or_candidate_execution(key, value):
    env = environment()
    env[key] = value
    with pytest.raises(ValueError, match="Diagnostic"):
        diagnostic.identity(env)


@pytest.mark.parametrize("event", ["push", "pull_request", "workflow_dispatch"])
@pytest.mark.parametrize("lifecycle", [False, True])
@pytest.mark.parametrize("mode", [False, True])
@pytest.mark.parametrize("probe", [False, True])
@pytest.mark.parametrize("subject", ["", "invalid"])
def test_lifecycle_excludes_every_other_job_without_changing_default_modes(
    event, lifecycle, mode, probe, subject
):
    jobs = job_scalars(REPO_ROOT / ".github/workflows/tests.yml")
    args = {
        "event": event,
        "lifecycle": lifecycle,
        "mode": mode,
        "probe": probe,
        "subject": subject,
    }
    selected = event == "workflow_dispatch" and lifecycle
    assert bool(expression(jobs["lifecycle-diagnostic"]["if"], **args)) is selected
    source = event == "workflow_dispatch" and not lifecycle and (mode or bool(subject))
    probing = event == "workflow_dispatch" and not lifecycle and probe and not source
    assert bool(expression(jobs["source-validation"]["if"], **args)) is source
    assert bool(expression(jobs["visual-isolation-probe"]["if"], **args)) is probing
    for name in ("tests", "quality", "fast-checks"):
        assert bool(expression(jobs[name]["if"], **args)) is not (selected or source or probing)
        if selected:
            assert expression(jobs[name]["name"], **args).startswith("Lifecycle-diagnostic-only")


def write_adapter_fixture(data, binding, expected, *, failed_baseline=False):
    process = {"exit_code": 0, "timed_out": False, "descendants_remaining": False}
    result = {
        "schema_version": 1,
        "purpose": binding["purpose"],
        "binding": binding,
        "complete": True,
        "errors": {},
        **diagnostic.AUTHORITY,
        "dependency_freeze": process,
        "ruff": [
            {"arguments": args, **process}
            for args in (["check", "--output-format", "full"], ["format", "--check", "--diff"])
        ],
    }
    for variant in ("baseline", "observed"):
        (data / variant).mkdir()
        code = 1 if failed_baseline and variant == "baseline" else 0
        summary = {
            "binding": binding,
            "variant": variant,
            "collected_case_ids": expected,
            "expected_case_ids": expected,
            "pytest_exit_code": code,
            "complete": True,
            "errors": {},
            "missing_phases": [],
            "reports": [
                {
                    "nodeid": case,
                    "when": when,
                    "outcome": "failed" if code and when == "call" else "passed",
                }
                for case in expected
                for when in ("setup", "call", "teardown")
            ],
        }
        (data / variant / "summary.json").write_text(json.dumps(summary))
        result[variant] = {**summary, "process": {**process, "exit_code": code}}
    (data / "result.json").write_text(json.dumps(result))


class OwnedSandbox:
    def __init__(self, fixture_state, candidate, trusted, journal, *, python_version):
        self.state = fixture_state
        self.candidate = candidate
        self.containers = []
        self.network_id = None
        self.rules = []
        self.image = "sha256:" + "f" * 64
        self.owner = "fixture-owned"
        assert trusted == self.state.roots["control"]
        assert python_version == "3.11"
        journal.write_text('{"owner":"fixture-owned"}')

    def setup(self):
        self.state.events.append(("setup",))

    def create(self, phase, command, binding):
        assert binding["candidate"] == diagnostic.SUBJECT
        self.state.events.append(("create", phase, command))
        self.containers.append(phase)
        return phase

    def verify(self):
        return None

    def validate_container(self, container):
        assert container in self.containers

    def remove(self, container):
        self.state.events.append(("remove", container))
        self.containers.remove(container)

    def cleanup(self):
        self.state.events.append(("cleanup",))
        self.containers.clear()
        if self.state.failure == "cleanup":
            message = "Uncertain resource removal"
            raise ValueError(message)

    def uncertain_operations(self):
        return [{"state": "uncertain"}] if self.state.failure == "uncertain" else []


class CaptureStreams:
    def __init__(self, state):
        self.state = state

    def __call__(self, container, phase, destination, timeout, **kwargs):
        state = self.state
        failure = state.failure
        assert container == phase
        assert timeout == (600 if phase == "install" else 900)
        assert 0 <= kwargs["limit"] <= 64 * diagnostic.MIB
        state.events.append(("drain", phase))
        if phase == "capture":
            candidate = next(state.roots["temporary"].glob("atk-lifecycle-private-*/subject"))
            data = candidate / "lifecycle-diagnostics"
            data.mkdir()
            binding = {
                **diagnostic.identity(state.env),
                "public_context_sha256": state.closure["public_context_sha256"],
                "adapter_sha256": state.closure["adapter_sha256"],
            }
            write_adapter_fixture(
                data,
                binding,
                state.closure["expected_case_ids"],
                failed_baseline=failure == "baseline",
            )
        phase_ok = failure != "test" and not (failure == "baseline" and phase == "capture")
        return {
            "phase": phase,
            "streams": {},
            "exit_code": 0 if phase_ok else 1,
            "readers_stopped": failure != "reader",
            "logs_safe": failure != "writer",
            "quarantined_logs": "private" if failure == "quarantine" else None,
            "outputs_complete": True,
            "ok": phase_ok,
        }


def fixture(tmp_path, monkeypatch, *, failure=None):
    env = environment()
    roots = {name: tmp_path / name for name in ("control", "subject", "temporary")}
    for name, root in roots.items():
        root.mkdir()
        if name != "temporary":
            (root / ".git").mkdir()
            (root / ".git/config").write_text("[credential]\nhelper = private-fixture\n")
    output = tmp_path / "output"
    env.update(
        RUNNER_TEMP=str(roots["temporary"]),
        DIAGNOSTIC_CONTROL_ROOT=str(roots["control"]),
        DIAGNOSTIC_SUBJECT_ROOT=str(roots["subject"]),
        GITHUB_OUTPUT=str(output),
    )
    events = []
    heads = {roots["control"]: env["GITHUB_SHA"], roots["subject"]: diagnostic.SUBJECT}

    def git(root, _env):
        assert not any(item[0] == "create" for item in events)
        return heads[root]

    fixture_state = SimpleNamespace(roots=roots, events=events, failure=failure)
    sandbox = SimpleNamespace(
        Sandbox=lambda *args, **kwargs: OwnedSandbox(fixture_state, *args, **kwargs)
    )
    isolation = SimpleNamespace(
        trusted_module=lambda name: artifact if name == "capture_artifact" else sandbox,
        collect=containment.collect,
        UNSAFE_LOG_WRITER=threading.Event(),
    )
    closure = {
        "adapter_sha256": "b" * 64,
        "public_context_sha256": "c" * 64,
        "subject_files": [],
        "expected_case_ids": ["case-" + str(i) for i in range(5)],
    }

    isolation.contained_output = CaptureStreams(
        SimpleNamespace(roots=roots, env=env, events=events, closure=closure, failure=failure)
    )
    monkeypatch.setattr(diagnostic, "git_head", git)
    monkeypatch.setattr(diagnostic, "source_closure", lambda _root: (isolation, closure))
    monkeypatch.setattr(diagnostic.signal, "alarm", lambda _seconds: None)
    assert diagnostic.guard(env)
    guard_root = Path(output.read_text().strip().split("=", 1)[1])
    env["DIAGNOSTIC_METADATA_ROOT"] = str(guard_root)
    return SimpleNamespace(env=env, events=events, roots=roots, output=output, guard=guard_root)


def test_real_orchestration_uploads_only_new_closed_root_and_preserves_pristine_subject(
    tmp_path, monkeypatch
):
    state = fixture(tmp_path, monkeypatch)
    original = (state.roots["subject"] / ".git/config").read_bytes()
    guard_bytes = (state.guard / "guard.json").read_bytes()
    assert diagnostic.run(state.env)
    lines = dict(line.split("=", 1) for line in state.output.read_text().splitlines())
    root = Path(lines["artifact_root"])
    assert root != state.guard
    assert root.name.startswith("atk-lifecycle-artifact-")
    assert (state.guard / "guard.json").read_bytes() == guard_bytes
    assert (state.roots["subject"] / ".git/config").read_bytes() == original
    created = [event for event in state.events if event[0] == "create"]
    assert [item[1] for item in created] == ["install", "capture"]
    assert "ruff==0.15.22" in created[0][2][-1]
    assert created[1][2][:3] == [
        "/work/.venv/bin/python",
        "-I",
        "/trusted/.github/agent/lifecycle_diagnostic_pytest.py",
    ]
    manifest = json.loads((root / "manifest.json").read_text())
    assert manifest["collection_complete"]
    assert manifest["job_id"] is None
    assert manifest["final_job_conclusion"] is None
    assert all(manifest[name] is False for name in diagnostic.AUTHORITY)
    assert {row["path"] for row in manifest["copied"]} == {
        str(path.relative_to(root))
        for path in root.rglob("*")
        if path.is_file() and path.name != "manifest.json"
    }


@pytest.mark.parametrize("failure", ["cleanup", "uncertain", "reader", "writer", "quarantine"])
def test_uncertain_cleanup_or_writers_never_emit_candidate_upload_root(
    tmp_path, monkeypatch, failure
):
    state = fixture(tmp_path, monkeypatch, failure=failure)
    assert not diagnostic.run(state.env)
    assert "artifact_root=" not in state.output.read_text()
    assert "metadata_root=" in state.output.read_text()
    assert ("cleanup",) in state.events


def test_failed_install_remains_failure_with_explicit_missing_data(tmp_path, monkeypatch):
    state = fixture(tmp_path, monkeypatch, failure="test")
    assert not diagnostic.run(state.env)
    output = dict(line.split("=", 1) for line in state.output.read_text().splitlines())
    value = json.loads((Path(output["artifact_root"]) / "manifest.json").read_text())
    assert value["phases"][0]["exit_code"] == 1
    assert value["data_missing"]
    assert not value["collection_complete"]


def test_complete_failed_baseline_is_retained_when_observed_run_passes(tmp_path, monkeypatch):
    state = fixture(tmp_path, monkeypatch, failure="baseline")
    assert not diagnostic.run(state.env)
    output = dict(line.split("=", 1) for line in state.output.read_text().splitlines())
    value = json.loads((Path(output["artifact_root"]) / "manifest.json").read_text())
    assert value["collection_complete"]
    assert not value["ok"]
    assert not value["diagnostic_passed"]
    assert value["variant_summaries"]["baseline"]["pytest_exit_code"] == 1
    assert value["variant_summaries"]["observed"]["pytest_exit_code"] == 0


@pytest.mark.parametrize(
    "kind", ["symlink", "hardlink", "fifo", "uppercase", "control", "unexpected"]
)
def test_stopped_data_inventory_refuses_unsafe_nodes_or_unexpected_names(tmp_path, kind):
    source = tmp_path / "data"
    source.mkdir()
    (source / "result.json").write_text("{}")
    if kind == "symlink":
        (source / "alias.json").symlink_to(source / "result.json")
    elif kind == "hardlink":
        os.link(source / "result.json", source / "alias.json")
    elif kind == "fifo":
        os.mkfifo(source / "pipe.json")
    else:
        name = {"uppercase": "OTHER.json", "control": "bad\n.json", "unexpected": "script.sh"}[kind]
        (source / name).write_text("{}")
    with pytest.raises(ValueError, match="diagnostic"):
        diagnostic.bounded_inventory(
            source, artifact, byte_limit=32 * diagnostic.MIB, file_limit=224, kind="data"
        )


def test_partitions_and_original_shared_helpers_remain_fixed():
    assert (diagnostic.LOG_LIMIT, diagnostic.DATA_LIMIT, diagnostic.META_LIMIT) == (
        64 * diagnostic.MIB,
        32 * diagnostic.MIB,
        4 * diagnostic.MIB,
    )
    assert (artifact.MAX_FILE, artifact.MAX_TOTAL, artifact.MAX_FILES) == (
        8 * diagnostic.MIB,
        64 * diagnostic.MIB,
        256,
    )
    for name, expected in diagnostic.SOURCES.items():
        assert (
            hashlib.sha256((REPO_ROOT / ".github/agent" / name).read_bytes()).hexdigest()
            == expected
        )


def test_every_fixed_adapter_framework_filename_is_admitted_without_importing_subject():
    adapter = controller.trusted_module("lifecycle_diagnostic_pytest")
    for profile in ("3.11", "3.13"):
        names = (*adapter.FRAMEWORK_MODULES, adapter.FRAMEWORK_LAYOUTS[profile])
        assert len(names) <= 16
        assert all(diagnostic.data_name("framework/" + name + ".py") for name in names)
    assert not diagnostic.data_name("framework/unreviewed.py")
    assert not diagnostic.data_name("arbitrary.py")


def test_actual_source_closure_binds_adapter_and_complete_public_archives():
    module, closure = diagnostic.source_closure(REPO_ROOT)
    assert callable(module.contained_output)
    assert closure["adapter_sha256"] == diagnostic.ADAPTER_SHA256
    assert closure["shared_sources"] == diagnostic.SOURCES
    assert len(closure["subject_files"]) == 15
    assert len(closure["expected_case_ids"]) == 5


def test_exact_public_parent_and_context_provenance():
    folder = REPO_ROOT / diagnostic.ARCHIVES
    parent = (folder / "parent-pr39-task.json").read_bytes()
    assert len(parent) == 34199
    assert hashlib.sha256(parent).hexdigest() == diagnostic.PARENT_DIGEST
    context_bytes = (folder / "pr39-public-planning-context.json").read_bytes()
    provenance = json.loads((folder / "baseline-provenance.json").read_text())
    assert provenance["public_context_sha256"] == hashlib.sha256(context_bytes).hexdigest()
    context = json.loads(context_bytes)
    assert {c["id"] for c in context["public_comments"]} == {
        6247967847,
        6248549284,
        6249025614,
        6253938144,
    }
    assert not context["private_originals_included"]
    forbidden = {
        "native_plan",
        "checkpoint_head",
        "checkpoint_base",
        "registry",
        "launch_id",
        "control_snapshot",
    }

    def check(value):
        if isinstance(value, dict):
            assert not forbidden.intersection(value)
            for item in value.values():
                check(item)
        elif isinstance(value, list):
            for item in value:
                check(item)

    check(context)
