"""Offline transparency and outcome retention for the fixed lifecycle adapter."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from textual.css.query import NoMatches

from tests._support.paths import REPO_ROOT


def load():
    spec = importlib.util.spec_from_file_location(
        "lifecycle_adapter", REPO_ROOT / ".github/agent/lifecycle_diagnostic_pytest.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def binding(module):
    return {
        "schema_version": 1,
        "purpose": module.PURPOSE,
        "repository": "Karapsin/analytics_toolkit",
        "candidate": module.SUBJECT,
        "workflow_sha": "a" * 40,
        "run_id": 123,
        "run_attempt": 1,
        "python_line": "3.11",
        "parent_task_id": module.PARENT_TASK,
        "parent_task_digest": module.PARENT_DIGEST,
        "new_task_id": module.NEW_TASK,
        "public_context_sha256": "b" * 64,
        "adapter_sha256": hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest(),
    }


def writer(module, tmp_path):
    output = (tmp_path / module.OUTPUT_NAME).resolve()
    output.mkdir()
    return module.Writer(output)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("schema_version", True),
        ("candidate", "b" * 40),
        ("repository", "foreign/repository"),
        ("python_line", "3.12"),
        ("python_line", []),
        ("run_id", True),
        ("run_attempt", 0),
        ("parent_task_digest", "c" * 64),
        ("public_context_sha256", "c" * 40),
    ],
)
def test_binding_refuses_changed_subject_scope_and_identity(key, value):
    module = load()
    original = binding(module)
    assert module.parse_binding(json.dumps(original)) == original
    original[key] = value
    with pytest.raises(ValueError, match=r"[Ll]ifecycle|binding"):
        module.parse_binding(json.dumps(original))


def test_binding_rejects_duplicates_unknown_fields_and_oversize():
    module = load()
    payload = json.dumps(binding(module))
    for raw in (payload[:-1] + ', "run_id": 123}', payload[:-1] + ', "command": "x"}', " " * 4097):
        with pytest.raises(ValueError, match=r"[Ll]ifecycle|binding"):
            module.parse_binding(raw)


def test_observers_preserve_sync_async_return_and_exception_identity(tmp_path, monkeypatch):
    module = load()
    output = writer(module, tmp_path)
    monkeypatch.setattr(module, "snapshot", lambda *_args: {"actual": True})
    observer = module.Observer(output, lambda instance, _args: (instance, instance))
    sentinel, instance = object(), object()
    original_error = ValueError("original application failure")
    calls = []

    def original(owner, *args, **kwargs):
        calls.append((owner, args, kwargs))
        return sentinel

    async def awaited(owner, *args, **kwargs):
        return original(owner, *args, **kwargs)

    assert observer.wrap(original, "sync")(instance, sentinel, flag=sentinel) is sentinel
    assert (
        asyncio.run(observer.wrap(awaited, "async")(instance, sentinel, flag=sentinel)) is sentinel
    )
    assert calls == [(instance, (sentinel,), {"flag": sentinel})] * 2

    def broken(_owner):
        raise original_error

    with pytest.raises(ValueError, match="original application failure") as raised:
        observer.wrap(broken, "broken")(instance)
    assert raised.value is original_error
    events = [
        json.loads(line)
        for p in output.root.glob("observed/events-*.jsonl")
        for line in p.read_text().splitlines()
    ]
    assert [event["stage"] for event in events] == [
        "before",
        "return",
        "before",
        "settled-return",
        "before",
        "exception",
    ]
    assert [event["sequence"] for event in events] == list(range(1, 7))


def test_logging_and_snapshot_failures_do_not_enter_application(tmp_path, monkeypatch):
    module = load()
    output = writer(module, tmp_path)
    observer = module.Observer(output, lambda *_args: (None, None))
    sentinel, original_error = object(), RuntimeError("original")

    def fail(*_args, **_kwargs):
        message = "diagnostic writer failed"
        raise OSError(message)

    monkeypatch.setattr(module, "snapshot", fail)
    assert observer.wrap(lambda _self: sentinel, "snapshot")(None) is sentinel
    monkeypatch.setattr(output, "record", fail)
    assert observer.wrap(lambda _self: sentinel, "writer")(None) is sentinel

    def original(_self):
        raise original_error

    with pytest.raises(RuntimeError, match="original") as raised:
        observer.wrap(original, "exception")(None)
    assert raised.value is original_error
    assert output.errors["snapshot:OSError"]
    assert output.errors["observation:OSError"]


def test_deferred_scheduler_restores_instance_and_delegates_callback_once(tmp_path, monkeypatch):
    module = load()
    output = writer(module, tmp_path)
    monkeypatch.setattr(module, "snapshot", lambda *_args: {})
    observer = module.Observer(output, lambda owner, _args: (owner, owner))
    pending, invoked = [], []
    sentinel = object()

    class Application:
        def call_after_refresh(self, callback):
            pending.append(callback)

        def _schedule_completion_position(self):
            def reposition():
                invoked.append("original")
                return sentinel

            self.call_after_refresh(reposition)
            return sentinel

    app = Application()
    wrapped = observer.wrap(Application._schedule_completion_position, "schedule", schedule=True)
    assert wrapped(app) is sentinel
    assert "call_after_refresh" not in vars(app)
    assert len(pending) == 1
    assert pending.pop()() is sentinel
    assert invoked == ["original"]
    app.call_after_refresh = pending.append
    prior = app.call_after_refresh
    assert wrapped(app) is sentinel
    assert app.call_after_refresh is prior
    assert pending.pop()() is sentinel
    assert invoked == ["original", "original"]


def test_writer_bounds_fail_explicitly_and_reserve_final_status(tmp_path, monkeypatch):
    module = load()
    output = writer(module, tmp_path)
    monkeypatch.setattr(module, "MAX_TOTAL", module.FINAL_RESERVE + 20)
    assert output.put("versions.json", b"x" * 20)
    assert not output.put("overflow.json", b"x")
    output.record("observed/events", {"too": "large"})
    assert output.errors["capacity"]
    assert output.json("result.json", {"complete": False}, final=True)
    assert not output.put("../escape.json", b"x")
    assert not output.put("unapproved.json", b"x", final=True)


def test_writer_rejects_linked_directories_and_changed_record_file(tmp_path):
    module = load()
    output = writer(module, tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (output.root / "framework").symlink_to(outside, target_is_directory=True)
    assert not output.put("framework/source.py", b"source")
    assert not list(outside.iterdir())
    output.record("baseline/reports", {"first": True})
    report = next(output.root.glob("baseline/reports-*.jsonl"))
    report.write_text("modified")
    output.record("baseline/reports", {"second": True})
    assert output.errors["ValueError"]


def test_reporter_distinguishes_failed_skipped_and_unexecuted_cases(tmp_path):
    module = load()
    output = writer(module, tmp_path)
    reports = module.Reports(output, "baseline")
    reports.collected = list(module.EXPECTED)
    for node in module.EXPECTED:
        for when in ("setup", "call", "teardown"):
            reports.pytest_runtest_logreport(
                SimpleNamespace(
                    nodeid=node,
                    when=when,
                    outcome="passed",
                    duration=0.0,
                    longreprtext="",
                    sections=[],
                )
            )
    assert reports.summary(0)["tests_passed"]
    reports.reports[1]["outcome"] = "failed"
    assert reports.summary(1)["complete"]
    assert not reports.summary(1)["tests_passed"]
    reports.reports[1]["outcome"] = "skipped"
    assert not reports.summary(0)["tests_passed"]
    reports.reports.pop()
    assert not reports.summary(0)["complete"]
    assert reports.summary(0)["missing_phases"] == [
        {"nodeid": module.EXPECTED[-1], "phase": "teardown"}
    ]


def test_baseline_never_installs_application_observer(tmp_path, monkeypatch):
    module = load()
    output = writer(module, tmp_path)
    installed = []
    monkeypatch.setattr(module, "install_observer", lambda *_args: installed.append(True))
    module.Reports(output, "baseline", binding(module)).pytest_sessionstart(None)
    assert installed == []
    module.Reports(output, "observed", binding(module)).pytest_sessionstart(None)
    assert installed == [True]


def test_collection_guard_retains_wrong_actual_case_ids(tmp_path):
    module = load()
    output = writer(module, tmp_path)
    reports = module.Reports(output, "baseline")
    wrong = [SimpleNamespace(nodeid=node) for node in module.EXPECTED[:-1]]
    with pytest.raises(pytest.UsageError, match="exact five cases"):
        reports.pytest_collection_modifyitems(wrong)
    assert reports.collected == list(module.EXPECTED[:-1])
    assert not reports.summary(4)["complete"]


def test_missing_optional_source_provenance_is_explicit_not_an_exception(tmp_path):
    module = load()
    output = writer(module, tmp_path)
    hashes = module.source_hashes(tmp_path, output)
    assert set(hashes) == set(module.SOURCE_FILES)
    assert all(record == {"unavailable": "FileNotFoundError"} for record in hashes.values())
    assert output.errors["source:FileNotFoundError"] == len(module.SOURCE_FILES)


def test_timeout_signal_can_race_natural_process_exit(monkeypatch):
    module = load()
    waits = iter([module.subprocess.TimeoutExpired("fixed", 1), 0])

    def wait(**_kwargs):
        value = next(waits)
        if isinstance(value, BaseException):
            raise value
        return value

    child = SimpleNamespace(pid=12345, returncode=0, wait=wait)
    monkeypatch.setattr(module.subprocess, "Popen", lambda *_args, **_kwargs: child)

    def vanished(*_args):
        raise ProcessLookupError

    monkeypatch.setattr(module.os, "killpg", vanished)
    assert module.process(["fixed"], 1) == {
        "exit_code": 0,
        "timed_out": True,
        "descendants_remaining": False,
    }


@pytest.mark.parametrize(
    ("line", "version", "layout"),
    [("3.11", "0.73.0", "textual._layout"), ("3.13", "0.89.1", "textual.layout")],
)
def test_framework_layout_uses_only_the_fixed_profile_mapping(
    tmp_path, monkeypatch, line, version, layout
):
    module = load()
    output = writer(module, tmp_path)
    descriptor = binding(module)
    descriptor["python_line"] = line
    installed = tmp_path / "installed"
    installed.mkdir()
    (installed / (layout + ".py")).write_text("# Explicit synthetic installed-source fixture\n")
    versions = {"textual": version, "rich": "fixture", "pytest": "fixture", "ruff": "0.15.22"}
    monkeypatch.setattr(module, "installed_versions", lambda: (line, versions))
    requested = []

    def source(writer, name):
        requested.append(name)
        body = (
            (installed / (name + ".py")).read_bytes()
            if name in module.FRAMEWORK_LAYOUTS.values()
            else b"# framework fixture\n"
        )
        writer.put("framework/" + name + ".py", body)
        return {"module": name, "bytes": len(body), "sha256": hashlib.sha256(body).hexdigest()}

    monkeypatch.setattr(module, "framework_source", source)
    module.provenance(output, descriptor)
    assert not output.errors
    assert requested[-1] == layout
    assert len(requested) == len(set(requested)) <= 16
    assert set(requested) & set(module.FRAMEWORK_LAYOUTS.values()) == {layout}


def workspace(tab):
    region = SimpleNamespace(x=0, y=0, width=80, height=20, right=80, bottom=20)
    region.intersection = lambda _other: region
    scalar = SimpleNamespace(value=3, unit=SimpleNamespace(name="CELLS"))
    return SimpleNamespace(
        tab_id=tab,
        completion_epoch=1,
        is_mounted=True,
        is_attached=True,
        completion_menu=SimpleNamespace(
            parent=SimpleNamespace(content_region=region),
            region=region,
            is_mounted=True,
            is_attached=True,
            is_open=False,
            suggestions=(),
            styles=SimpleNamespace(display="block", width=scalar, height=scalar),
        ),
        editor=SimpleNamespace(
            scrollable_content_region=region,
            cursor_location=(0, 0),
            cursor_render_offset=(0, 0),
            scroll_x=0,
            scroll_y=0,
            gutter_width=1,
        ),
    )


def test_existing_pause_records_actual_readiness_and_removed_workspace(tmp_path):
    module = load()
    output = writer(module, tmp_path)
    old, new = workspace("old"), workspace("new")
    timer = SimpleNamespace(
        _interval=0.3,
        _repeat=0,
        _skip=True,
        _reset=False,
        _active=SimpleNamespace(is_set=lambda: True),
        _task=SimpleNamespace(done=lambda: False, cancelled=lambda: False),
        _callback=lambda: None,
    )
    app = SimpleNamespace(
        active_workspace=old,
        _workspaces={"old": old, "new": new},
        visual_scene_id="completion-columns",
        creation_scene_ready=True,
        _timers=[timer],
    )
    observer = module.Observer(output, module.observed_context, "3.11")

    def remove(owner, tab):
        owner._workspaces.pop(tab)
        old.is_attached = False
        owner.active_workspace = new

    sentinel = object()

    async def pause(_pilot):
        return sentinel

    observer.wrap(remove, "_remove_workspace")(app, "old")
    assert asyncio.run(observer.wrap(pause, "pause")(SimpleNamespace(_app=app))) is sentinel
    events = [
        json.loads(line)
        for p in output.root.glob("observed/events-*.jsonl")
        for line in p.read_text().splitlines()
    ]
    settled = events[-1]
    assert settled["stage"] == "settled-return"
    assert settled["observer_elapsed_ns"] >= 0
    assert settled["state"]["workspace_id"] == id(new)
    removed = settled["state"]["retained_removed_workspace"]
    assert removed["workspace_id"] == id(old)
    assert removed["workspace_mounted"] is True
    assert removed["workspace_attached"] is False
    assert removed["registered"] is False
    scene = settled["state"]["scene"]
    assert scene["creation_scene_ready"] is True
    assert settled["state"]["menu_open"] is False
    assert scene["readiness"].startswith("unknown:")
    assert scene["timers"][0]["interval"] == 0.3
    assert scene["timers"][0]["task_done"] is False


def test_plain_application_readiness_is_explicitly_unavailable():
    module = load()
    state = module.scene_timers(SimpleNamespace())
    assert state["scene_available"] is False
    assert state["timers_available"] is False
    assert state["creation_scene_ready"] is None


@pytest.mark.parametrize("missing", ["completion_menu", "editor"])
def test_removed_descendant_query_preserves_settled_lifecycle_state(tmp_path, monkeypatch, missing):
    module = load()
    output = writer(module, tmp_path)

    class Removed(SimpleNamespace):
        pass

    def pruned(_workspace):
        message = "Normal removed descendant"
        raise NoMatches(message)

    old = Removed(**vars(workspace("old")))
    monkeypatch.setattr(Removed, missing, property(pruned), raising=False)
    old.is_attached = False
    new = workspace("new")
    app = SimpleNamespace(active_workspace=new, _workspaces={"new": new})
    observer = module.Observer(output, module.observed_context)
    observer.removed_context = (app, old)

    async def pause(_pilot):
        return "original-return"

    assert (
        asyncio.run(observer.wrap(pause, "pause")(SimpleNamespace(_app=app))) == "original-return"
    )
    events = [
        json.loads(line)
        for p in output.root.glob("observed/events-*.jsonl")
        for line in p.read_text().splitlines()
    ]
    removed = events[-1]["state"]["retained_removed_workspace"]
    assert removed["workspace_id"] == id(old)
    assert not removed["active"]
    assert not removed["registered"]
    assert removed["workspace_mounted"]
    assert not removed["workspace_attached"]
    assert removed["epoch"] == 1
    assert removed["geometry_unavailable"] == "removed-descendant-no-matches"
    assert "menu" not in removed
    assert "viewport" not in removed
    assert not output.errors
    old.is_attached = True
    with pytest.raises(NoMatches, match="Normal removed descendant"):
        module.snapshot(app, old)


@pytest.mark.parametrize("launch_failure", [False, True])
def test_parent_retains_baseline_failure_observed_pass_and_both_ruff_statuses(
    tmp_path, monkeypatch, launch_failure
):
    module = load()
    descriptor = binding(module)
    root = tmp_path.resolve()
    commands = []
    monkeypatch.setattr(module, "provenance", lambda *_args: None)

    def execute(arguments, _timeout):
        commands.append(arguments)
        code = 0
        if "--variant" in arguments:
            mode = arguments[-1]
            original = 1 if mode == "baseline" else 0
            if launch_failure and mode == "baseline":
                message = "baseline launch failed"
                raise OSError(message)
            output = module.Writer(root / module.OUTPUT_NAME)
            reports = module.Reports(output, mode)
            reports.collected = list(module.EXPECTED)
            reports.reports = [
                {
                    "nodeid": node,
                    "when": phase,
                    "outcome": "failed" if original and phase == "call" else "passed",
                    "duration": 0.0,
                }
                for node in module.EXPECTED
                for phase in ("setup", "call", "teardown")
            ]
            output.json(
                mode + "/summary.json",
                {"binding": descriptor, **reports.summary(original)},
                final=True,
            )
            code = original
        elif "check" in arguments:
            code = 1
        return {"exit_code": code, "timed_out": False, "descendants_remaining": False}

    monkeypatch.setattr(module, "process", execute)
    assert not module.run(descriptor, root)
    result = json.loads((root / module.OUTPUT_NAME / "result.json").read_text())
    assert result["baseline"]["pytest_exit_code"] == (None if launch_failure else 1)
    assert result["observed"]["pytest_exit_code"] == 0
    assert [command[-1] for command in commands if "--variant" in command] == [
        "baseline",
        "observed",
    ]
    assert [entry["exit_code"] for entry in result["ruff"]] == [1, 0]
    assert not result["visual_approval"]
    assert not result["prerequisite_satisfied"]
