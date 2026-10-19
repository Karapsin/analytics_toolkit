"""Fixed, contained PR39 diagnostics; observations never grant acceptance."""

from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
import functools
import hashlib
import importlib
import importlib.metadata
import inspect
import itertools
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import threading
import time
from typing import Any

SUBJECT = "76eb8481734ec4ffb77d71ce300787caff21b860"
PARENT_TASK = "04c60facc8294087b55ffdd496a4de82"
PARENT_DIGEST = "813e95fe9b37968b03c3a016c0992531c4413807cc334ffe03f9e942a20e8b18"
NEW_TASK = "357a59078b4f423491e915d12ca37061"
PURPOSE = "pr39-lifecycle-baseline-diagnostic"
OUTPUT_NAME = "lifecycle-diagnostics"
SELECTORS = (
    "tests/agent_tools/visual_scene_states.py::test_completion_geometry_guard_rejects_overlapping_popup",
    "tests/sql/explorer/completion_placement.py::test_completion_repositions_on_scroll_resize_and_narrowing",
    "tests/sql/explorer/completion_placement.py::test_completion_clamps_right_edge_and_closes_without_row_space",
    "tests/sql/explorer/completion_placement.py::test_deferred_completion_layout_ignores_inactive_workspace",
)
EXPECTED = (*SELECTORS[:3], SELECTORS[3] + "[False]", SELECTORS[3] + "[True]")
RUFF_TARGETS = (
    "tests/sql/explorer/completion_placement.py",
    "tests/sql/explorer/journal_screen.py",
    "tests/agent_tools/visual_scene_states.py",
)
SOURCE_FILES = (
    *RUFF_TARGETS,
    "analytics_toolkit/sql_explorer/completion_commands.py",
    "analytics_toolkit/sql_explorer/tabs.py",
    "analytics_toolkit/sql_explorer/workspace.py",
    "analytics_toolkit/sql_explorer/app.py",
    "analytics_toolkit/sql_explorer/editor.py",
    "agent_tools/sql_explorer_visual_scene.py",
    "tests/sql/_support/completion.py",
    "tests/sql/_support/explorer.py",
    "tests/conftest.py",
    "pyproject.toml",
)
FRAMEWORK_MODULES = (
    "textual.dom",
    "textual.widget",
    "textual.pilot",
    "textual.reactive",
    "textual.message_pump",
    "textual.await_remove",
    "textual.containers",
    "textual._arrange",
    "textual.scroll_view",
    "textual.timer",
    "textual.widgets._text_area",
    "rich.cells",
)
FRAMEWORK_LAYOUTS = {"3.11": "textual._layout", "3.13": "textual.layout"}
BINDING_FIELDS = {
    "schema_version",
    "purpose",
    "repository",
    "candidate",
    "workflow_sha",
    "run_id",
    "run_attempt",
    "python_line",
    "parent_task_id",
    "parent_task_digest",
    "new_task_id",
    "public_context_sha256",
    "adapter_sha256",
}
MAX_FILE = 8 * 1024 * 1024
MAX_TOTAL = 32 * 1024 * 1024
MAX_FILES = 224
FINAL_RESERVE = 192 * 1024
FINAL_NAMES = {"result.json", "baseline/summary.json", "observed/summary.json"}


def parse_binding(raw: str) -> dict[str, Any]:
    """Accept only the reviewed purpose, subject, identities and two profiles."""
    if len(raw.encode("utf-8")) > 4096:
        raise ValueError("Lifecycle binding exceeds capacity")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate lifecycle binding field")
            result[key] = value
        return result

    value = json.loads(raw, object_pairs_hook=pairs)
    fixed = {
        "schema_version": 1,
        "purpose": PURPOSE,
        "repository": "Karapsin/analytics_toolkit",
        "candidate": SUBJECT,
        "parent_task_id": PARENT_TASK,
        "parent_task_digest": PARENT_DIGEST,
        "new_task_id": NEW_TASK,
    }
    if not isinstance(value, dict) or set(value) != BINDING_FIELDS:
        raise ValueError("Unexpected lifecycle binding fields")
    if type(value["schema_version"]) is not int or any(value[k] != v for k, v in fixed.items()):
        raise ValueError("Lifecycle binding differs from fixed subject")
    if not isinstance(value["python_line"], str) or value["python_line"] not in {"3.11", "3.13"}:
        raise ValueError("Unreviewed lifecycle profile")
    for key in ("run_id", "run_attempt"):
        if type(value[key]) is not int or not 0 < value[key] < 10**20:
            raise ValueError("Invalid lifecycle run identity")
    for key, length in (
        ("workflow_sha", 40),
        ("public_context_sha256", 64),
        ("adapter_sha256", 64),
    ):
        if (
            not isinstance(value[key], str)
            or re.fullmatch("[0-9a-f]{" + str(length) + "}", value[key]) is None
        ):
            raise ValueError("Invalid lifecycle source hash")
    return value


def regular(path: Path) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > MAX_FILE:
            raise ValueError("Unsafe lifecycle data file")
        body = stream.read(MAX_FILE + 1)
        after = os.fstat(stream.fileno())
        fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        if len(body) != before.st_size or any(
            getattr(before, key) != getattr(after, key) for key in fields
        ):
            raise ValueError("Lifecycle data changed during read")
    return body


class Writer:
    """One sequential process owns bounded files; final status has reserved space."""

    def __init__(self, root: Path):
        self.root = root
        self.errors: Counter[str] = Counter()
        self.seen_bytes = 0
        self.seen_hash = hashlib.sha256()
        self.parts: dict[str, tuple[str, int]] = {}
        self.lock = threading.RLock()
        self.refresh()

    def refresh(self) -> None:
        if self.root.is_symlink() or not self.root.is_dir() or self.root != self.root.resolve():
            raise ValueError("Unsafe lifecycle output root")
        self.total = self.files = 0
        for directory, dirs, files in os.walk(self.root, followlinks=False):
            for name in (*dirs, *files):
                path = Path(directory) / name
                relative = path.relative_to(self.root)
                if len(relative.parts) > 4 or any(
                    re.fullmatch(r"[a-z0-9_.-]+", p) is None for p in relative.parts
                ):
                    raise ValueError("Unexpected lifecycle output member")
                info = path.lstat()
                if stat.S_ISDIR(info.st_mode):
                    continue
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_FILE:
                    raise ValueError("Unsafe lifecycle output member")
                self.total += info.st_size
                self.files += 1
        if self.total > MAX_TOTAL or self.files > MAX_FILES:
            raise ValueError("Lifecycle output exceeds capacity")

    def error(self, reason: str) -> None:
        self.errors[reason] += 1

    def _parent(self, name: str):
        parts = Path(name).parts
        if (
            not parts
            or len(parts) > 4
            or any(re.fullmatch(r"[a-z0-9_.-]+", p) is None or p in {".", ".."} for p in parts)
        ):
            raise ValueError("Unsafe lifecycle writer path")
        descriptor = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in parts[:-1]:
                try:
                    os.mkdir(part, 0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
                child = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
                )
                os.close(descriptor)
                descriptor = child
            return descriptor, parts[-1]
        except BaseException:
            os.close(descriptor)
            raise

    def put(self, name: str, body: bytes, *, final: bool = False) -> bool:
        with self.lock:
            self.seen_bytes += len(body)
            self.seen_hash.update(body)
            reserve = 0 if final else FINAL_RESERVE
            slots = 0 if final else 3
            if (
                len(body) > (64 * 1024 if final else MAX_FILE)
                or self.total + len(body) > MAX_TOTAL - reserve
                or self.files >= MAX_FILES - slots
                or (final and name not in FINAL_NAMES)
            ):
                self.error("capacity")
                return False
            try:
                parent, leaf = self._parent(name)
                try:
                    descriptor = os.open(
                        leaf,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                        0o600,
                        dir_fd=parent,
                    )
                finally:
                    os.close(parent)
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(body)
                self.total += len(body)
                self.files += 1
            except (OSError, ValueError) as exc:
                self.error(type(exc).__name__)
                return False
            return True

    def json(self, name: str, value: Any, *, final: bool = False) -> bool:
        return self.put(
            name,
            (json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n").encode(),
            final=final,
        )

    def record(self, series: str, value: Any) -> None:
        body = (json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n").encode()
        with self.lock:
            self.seen_bytes += len(body)
            self.seen_hash.update(body)
            name, size = self.parts.get(series, ("", MAX_FILE))
            if not name or size + len(body) > MAX_FILE:
                number = sum(1 for p in self.root.glob(series + "-*.jsonl"))
                name, size = series + f"-{number:06d}.jsonl", 0
                if not self.put(name, b""):
                    return
                self.parts[series] = (name, size)
            if len(body) > MAX_FILE or self.total + len(body) > MAX_TOTAL - FINAL_RESERVE:
                self.error("capacity")
                return
            try:
                parent, leaf = self._parent(name)
                try:
                    descriptor = os.open(
                        leaf,
                        os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK,
                        dir_fd=parent,
                    )
                finally:
                    os.close(parent)
                with os.fdopen(descriptor, "ab") as stream:
                    info = os.fstat(stream.fileno())
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size != size:
                        raise ValueError("Lifecycle record file changed")
                    stream.write(body)
                self.total += len(body)
                self.parts[series] = (name, size + len(body))
            except (OSError, ValueError) as exc:
                self.error(type(exc).__name__)


def rectangle(value: Any) -> Any:
    return (
        None
        if value is None
        else {key: getattr(value, key) for key in ("x", "y", "width", "height", "right", "bottom")}
    )


def callback_name(callback):
    if isinstance(callback, functools.partial):
        return {
            "function": getattr(callback.func, "__qualname__", None),
            "callback_arguments": [
                getattr(arg, "__qualname__", None) for arg in callback.args[:2] if callable(arg)
            ],
        }
    return {"function": getattr(callback, "__qualname__", None)}


def scene_timers(app):
    scene = getattr(app, "visual_scene_id", None)
    timers = getattr(app, "_timers", None)
    records = []
    for timer in itertools.islice(timers or (), 32):
        task = timer._task
        records.append(
            {
                "timer_id": id(timer),
                "interval": timer._interval,
                "repeat": timer._repeat,
                "skip": timer._skip,
                "reset": timer._reset,
                "active": timer._active.is_set(),
                "task_present": task is not None,
                "task_done": task.done() if task is not None else None,
                "task_cancelled": task.cancelled() if task is not None else None,
                "callback": callback_name(timer._callback),
            }
        )
    return {
        "scene_id": scene,
        "scene_available": scene is not None,
        "creation_scene_ready": getattr(app, "creation_scene_ready", None),
        "readiness": "unknown: use actual menu state; flags/task completion do not establish readiness",
        "timer_start_time": "not_observed",
        "timers": sorted(records, key=lambda r: r["timer_id"]),
        "timers_available": timers is not None,
        "timer_inventory_complete": timers is not None and len(timers) <= 32,
    }


def snapshot(app: Any, workspace: Any) -> dict[str, Any]:
    """Read known state only; normal unmount absence is explicit, never a fake box."""
    result: dict[str, Any] = {
        "app_id": id(app),
        "workspace_id": id(workspace),
        "scene": scene_timers(app),
    }
    if workspace is None:
        return {**result, "unavailable": "no-workspace"}
    result.update(
        active=workspace is app.active_workspace,
        registered=workspace.tab_id in app._workspaces,
        epoch=workspace.completion_epoch,
        workspace_mounted=workspace.is_mounted,
        workspace_attached=workspace.is_attached,
    )
    try:
        menu, editor = workspace.completion_menu, workspace.editor
    except Exception as exc:
        no_matches = importlib.import_module("textual.css.query").NoMatches
        if not isinstance(exc, no_matches) or result["registered"] or result["workspace_attached"]:
            raise
        result["geometry_unavailable"] = "removed-descendant-no-matches"
        return result
    parent = menu.parent
    result.update(
        menu_mounted=menu.is_mounted,
        menu_attached=menu.is_attached,
        menu_id=id(menu),
        menu_open=menu.is_open,
        suggestions_id=id(menu.suggestions),
        suggestions_count=len(menu.suggestions),
        parent=rectangle(parent.content_region) if parent is not None else None,
        viewport=rectangle(editor.scrollable_content_region),
        menu=rectangle(menu.region),
        cursor=list(editor.cursor_location),
        render_offset=list(editor.cursor_render_offset),
        scroll_x=float(editor.scroll_x),
        scroll_y=float(editor.scroll_y),
        gutter=editor.gutter_width,
        display=menu.styles.display,
    )
    for key in ("width", "height"):
        scalar = getattr(menu.styles, key)
        result[key] = (
            {"value": scalar.value, "unit": scalar.unit.name} if scalar is not None else None
        )
    if parent is not None:
        result["intersection"] = rectangle(
            editor.scrollable_content_region.intersection(parent.content_region)
        )
    return result


class Observer:
    """Transparent method delegates, with explicitly separate observation errors."""

    def __init__(self, writer: Writer, context, profile=""):
        self.writer, self.context = writer, context
        self.profile = profile
        self.sequence = 0
        self.started_ns = time.monotonic_ns()
        self.removed_context = None
        self.nodeid, self.phase = "", ""
        self.originals: list[tuple[Any, str, Any]] = []

    def emit(self, label, stage, context, **extra):
        try:
            with self.writer.lock:
                self._emit(label, stage, context, **extra)
        except Exception as exc:
            self.writer.error("observation:" + type(exc).__name__)

    def _emit(self, label, stage, context, **extra):
        try:
            self.sequence += 1
            try:
                state = snapshot(*context) if context is not None else {"unavailable": "no-context"}
                if (
                    self.removed_context is not None
                    and context is not None
                    and self.removed_context[0] is context[0]
                ):
                    state["retained_removed_workspace"] = snapshot(*self.removed_context)
            except Exception as exc:
                state = {"unavailable": type(exc).__name__}
                self.writer.error("snapshot:" + type(exc).__name__)
            observed_ns = time.monotonic_ns()
            self.writer.record(
                "observed/events",
                {
                    "sequence": self.sequence,
                    "monotonic_ns": observed_ns,
                    "observer_elapsed_ns": observed_ns - self.started_ns,
                    "variant": "observed",
                    "python_line": self.profile,
                    "nodeid": self.nodeid,
                    "phase": self.phase,
                    "method": label,
                    "stage": stage,
                    "state": state,
                    **extra,
                },
            )
        except Exception as exc:
            self.writer.error("observation:" + type(exc).__name__)

    def context_for(self, instance, args):
        try:
            return self.context(instance, args)
        except Exception as exc:
            self.writer.error("context:" + type(exc).__name__)
            return None

    def callback(self, original, context):
        @functools.wraps(original)
        def delegated(*args, **kwargs):
            self.emit("reposition-callback", "before", context, callback_id=id(original))
            try:
                value = original(*args, **kwargs)
            except BaseException:
                self.emit("reposition-callback", "exception", context, callback_id=id(original))
                raise
            self.emit("reposition-callback", "return", context, callback_id=id(original))
            return value

        return delegated

    @contextmanager
    def scheduler(self, instance, context):
        try:
            namespace = vars(instance)
            missing = object()
            previous = namespace.get("call_after_refresh", missing)
            original = instance.call_after_refresh
        except Exception as exc:
            self.writer.error("scheduler:" + type(exc).__name__)
            yield
            return

        def delegate(callback, *args, **kwargs):
            if getattr(callback, "__qualname__", "").endswith(
                "._schedule_completion_position.<locals>.reposition"
            ):
                callback = self.callback(callback, context)
            return original(callback, *args, **kwargs)

        namespace["call_after_refresh"] = delegate
        try:
            yield
        finally:
            if previous is missing:
                namespace.pop("call_after_refresh", None)
            else:
                namespace["call_after_refresh"] = previous

    def wrap(self, original, label, *, schedule=False):
        if inspect.iscoroutinefunction(original):

            @functools.wraps(original)
            async def awaited(instance, *args, **kwargs):
                context = self.context_for(instance, args)
                self.emit(label, "before", context)
                try:
                    value = await original(instance, *args, **kwargs)
                except BaseException:
                    self.emit(label, "exception", context)
                    raise
                self.emit(label, "settled-return", context)
                return value

            return awaited

        @functools.wraps(original)
        def delegated(instance, *args, **kwargs):
            context = self.context_for(instance, args)
            if label == "_remove_workspace":
                self.removed_context = context
            self.emit(label, "before", context)
            try:
                if schedule:
                    with self.scheduler(instance, context):
                        value = original(instance, *args, **kwargs)
                else:
                    value = original(instance, *args, **kwargs)
            except BaseException:
                self.emit(label, "exception", context)
                raise
            self.emit(label, "return", context)
            return value

        return delegated

    def patch(self, owner, name, *, schedule=False):
        original = getattr(owner, name)
        self.originals.append((owner, name, original))
        setattr(owner, name, self.wrap(original, name, schedule=schedule))

    def restore(self):
        for owner, name, original in reversed(self.originals):
            setattr(owner, name, original)
        self.originals.clear()


def observed_context(instance, args):
    app = getattr(instance, "_app", None) or getattr(instance, "app", instance)
    if args and hasattr(args[0], "completion_menu"):
        workspace = args[0]
    elif args and isinstance(args[0], str):
        workspace = app._workspaces.get(args[0])
    else:
        workspace = app.active_workspace
    return app, workspace


def install_observer(writer, profile):
    from analytics_toolkit.sql_explorer.completion_commands import (
        SqlExplorerCompletionCommandsMixin,
    )
    from analytics_toolkit.sql_explorer.tabs import SqlExplorerTabCommandsMixin
    from analytics_toolkit.sql_explorer.workspace import QueryPane, workspace_for
    from textual.pilot import Pilot

    def context(instance, args):
        if isinstance(instance, QueryPane):
            return instance.app, workspace_for(instance)
        return observed_context(instance, args)

    observer = Observer(writer, context, profile)
    try:
        for name in (
            "_schedule_completion_position",
            "_position_completion",
            "_refresh_open_completion",
        ):
            observer.patch(
                SqlExplorerCompletionCommandsMixin,
                name,
                schedule=name == "_schedule_completion_position",
            )
        observer.patch(SqlExplorerTabCommandsMixin, "_remove_workspace")
        observer.patch(QueryPane, "on_resize")
        for name in ("press", "pause", "resize_terminal"):
            observer.patch(Pilot, name)
    except Exception:
        observer.restore()
        raise
    return observer


class Reports:
    """Pytest reporting in both runs; baseline never installs an observer."""

    def __init__(self, writer, variant, binding=None):
        self.writer, self.variant = writer, variant
        self.binding = binding or {}
        self.collected: list[str] = []
        self.reports: list[dict[str, Any]] = []
        self.observer = None

    def _record(self, value):
        try:
            self.writer.record(
                self.variant + "/reports", {"monotonic_ns": time.monotonic_ns(), **value}
            )
        except Exception as exc:
            self.writer.error("report:" + type(exc).__name__)

    def pytest_configure(self, config):
        try:
            plugins = []
            for name, plugin in config.pluginmanager.list_name_plugin():
                plugins.append(
                    {
                        "name": name,
                        "module": getattr(plugin, "__module__", None),
                        "file": getattr(plugin, "__file__", None),
                    }
                )
            self._record(
                {
                    "kind": "configuration",
                    "root": str(config.rootpath),
                    "config": str(config.inipath),
                    "plugins": plugins,
                }
            )
        except Exception as exc:
            self.writer.error("configuration:" + type(exc).__name__)

    def pytest_sessionstart(self, session):
        if self.variant == "observed":
            try:
                self.observer = install_observer(self.writer, self.binding["python_line"])
            except Exception as exc:
                self.writer.error("observer-install:" + type(exc).__name__)

    def pytest_collection_modifyitems(self, items):
        import pytest

        self.collected = [item.nodeid for item in items]
        self._record({"kind": "collection", "nodeids": self.collected})
        if tuple(self.collected) != EXPECTED:
            raise pytest.UsageError("Fixed lifecycle collection differs from the exact five cases")

    def pytest_collectreport(self, report):
        try:
            self._record(
                {
                    "kind": "collection-report",
                    "nodeid": report.nodeid,
                    "outcome": report.outcome,
                    "longrepr": report.longreprtext,
                    "sections": report.sections,
                }
            )
        except Exception as exc:
            self.writer.error("collection-report:" + type(exc).__name__)

    def _phase(self, item, phase):
        if self.observer is not None:
            self.observer.nodeid, self.observer.phase = item.nodeid, phase
        self._record({"kind": "phase-start", "nodeid": item.nodeid, "phase": phase})

    def pytest_runtest_setup(self, item):
        self._phase(item, "setup")

    def pytest_runtest_call(self, item):
        self._phase(item, "call")

    def pytest_runtest_teardown(self, item, nextitem):
        self._phase(item, "teardown")

    def pytest_runtest_logreport(self, report):
        entry = {
            "nodeid": report.nodeid,
            "when": report.when,
            "outcome": report.outcome,
            "duration": report.duration,
        }
        self.reports.append(entry)
        try:
            self._record(
                {
                    "kind": "report",
                    **entry,
                    "longrepr": report.longreprtext,
                    "sections": report.sections,
                }
            )
        except Exception as exc:
            self.writer.error("report:" + type(exc).__name__)

    def summary(self, exit_code):
        phases = Counter((r["nodeid"], r["when"]) for r in self.reports)
        missing = [
            {"nodeid": node, "phase": phase}
            for node in EXPECTED
            for phase in ("setup", "call", "teardown")
            if not phases[node, phase]
        ]
        complete = (
            tuple(self.collected) == EXPECTED
            and not missing
            and all(count == 1 for count in phases.values())
            and set(phases) == {(n, p) for n in EXPECTED for p in ("setup", "call", "teardown")}
        )
        return {
            "variant": self.variant,
            "pytest_exit_code": exit_code,
            "expected_case_ids": list(EXPECTED),
            "collected_case_ids": self.collected,
            "reports": self.reports,
            "missing_phases": missing,
            "complete": complete and not self.writer.errors,
            "tests_passed": complete
            and exit_code == 0
            and all(r["outcome"] == "passed" for r in self.reports),
            "errors": dict(self.writer.errors),
            "seen_bytes": self.writer.seen_bytes,
            "seen_sha256": self.writer.seen_hash.hexdigest(),
        }


def source_hashes(root, writer):
    result = {}
    for name in SOURCE_FILES:
        try:
            result[name] = hashlib.sha256(regular(root / name)).hexdigest()
        except (OSError, ValueError) as exc:
            writer.error("source:" + type(exc).__name__)
            result[name] = {"unavailable": type(exc).__name__}
    return result


def provenance(writer, binding):
    line, versions = installed_versions()
    textual = tuple(int(x) for x in versions["textual"].split(".")[:3])
    valid = (line == "3.11" and (0, 73) <= textual[:2] < (0, 74)) or (
        line == "3.13" and (0, 89, 1) <= textual < (0, 90)
    )
    if line != binding["python_line"] or not valid or versions["ruff"] != "0.15.22":
        raise ValueError("Installed lifecycle profile differs from its declared versions")
    writer.json("versions.json", {"python": sys.version, "packages": versions})
    files = []
    for name in (*FRAMEWORK_MODULES, FRAMEWORK_LAYOUTS[binding["python_line"]]):
        try:
            files.append(framework_source(writer, name))
        except (ImportError, OSError, ValueError, TypeError) as exc:
            writer.error("framework:" + type(exc).__name__)
            files.append({"module": name, "unavailable": type(exc).__name__})
    writer.json("framework/index.json", files)


def installed_versions():
    versions = {
        name: importlib.metadata.version(name) for name in ("textual", "rich", "pytest", "ruff")
    }
    return ".".join(map(str, sys.version_info[:2])), versions


def framework_source(writer, name):
    module = importlib.import_module(name)
    path = Path(inspect.getfile(module)).resolve()
    distribution = importlib.metadata.distribution(name.split(".")[0])
    boundary = Path(distribution.locate_file(name.split(".")[0])).resolve()
    if boundary not in path.parents or path.suffix != ".py":
        raise ValueError("Framework source is outside the installed distribution")
    body = regular(path)
    writer.put("framework/" + name + ".py", body)
    return {
        "module": name,
        "loaded_path": str(path),
        "bytes": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
    }


def variant(binding, mode, root=Path("/work")):
    import pytest

    writer = Writer(root / OUTPUT_NAME)
    reports = Reports(writer, mode, binding)
    before = source_hashes(root, writer)
    writer.json(mode + "/source-before.json", before)
    # Only contained code gets a candidate search path. The host never imports it.
    sys.path.insert(0, str(root))
    os.chdir(root)
    exit_code = None
    try:
        exit_code = int(
            pytest.main([*SELECTORS, "-vv", "--tb=long", "-rA", "--capture=no"], plugins=[reports])
        )
    finally:
        if reports.observer is not None:
            try:
                reports.observer.restore()
            except Exception as exc:
                writer.error("observer-restore:" + type(exc).__name__)
        try:
            after = source_hashes(root, writer)
            writer.json(mode + "/source-after.json", after)
            if after != before:
                writer.error("source-drift")
        except (OSError, ValueError) as exc:
            writer.error("source:" + type(exc).__name__)
        result = {"binding": binding, **reports.summary(exit_code)}
        if not writer.json(mode + "/summary.json", result, final=True):
            raise ValueError("Could not retain lifecycle variant summary")
    return result["tests_passed"] and result["complete"]


def process(arguments, timeout):
    """Inherit complete streams; a timeout retains its original status."""
    print(json.dumps({"lifecycle_command": arguments, "stage": "start"}), flush=True)
    child = subprocess.Popen(arguments, start_new_session=True)
    timed_out = False
    try:
        code = child.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        signal_group(child.pid, signal.SIGTERM)
        try:
            child.wait(timeout=15)
        except subprocess.TimeoutExpired:
            signal_group(child.pid, signal.SIGKILL)
            child.wait(timeout=15)
        code = child.returncode
    descendants = False
    try:
        os.killpg(child.pid, 0)
    except ProcessLookupError:
        pass
    else:
        descendants = True
        signal_group(child.pid, signal.SIGKILL)
    result = {"exit_code": code, "timed_out": timed_out, "descendants_remaining": descendants}
    print(json.dumps({"lifecycle_command": arguments, "stage": "end", **result}), flush=True)
    return result


def signal_group(pid, number):
    try:
        os.killpg(pid, number)
    except ProcessLookupError:
        pass  # Natural exit can race a bounded timeout/cleanup signal.


def attempt_process(arguments, timeout):
    try:
        return process(arguments, timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "exit_code": None,
            "timed_out": False,
            "descendants_remaining": True,
            "error_kind": type(exc).__name__,
        }


def read_summary(root, mode, binding):
    value = json.loads(regular(root / OUTPUT_NAME / mode / "summary.json"))
    if (
        not isinstance(value, dict)
        or value.get("binding") != binding
        or value.get("variant") != mode
        or value.get("expected_case_ids") != list(EXPECTED)
    ):
        raise ValueError("Lifecycle child summary binding differs")
    return value


def run(binding, root=Path("/work")):
    os.chdir(root)
    output = root / OUTPUT_NAME
    output.mkdir(mode=0o700)
    writer = Writer(output)
    result = {
        "schema_version": 1,
        "purpose": PURPOSE,
        "binding": binding,
        "visual_approval": False,
        "production_activation": False,
        "prerequisite_satisfied": False,
        "complete": False,
        "baseline": None,
        "observed": None,
        "ruff": [],
    }
    try:
        provenance(writer, binding)
        result["dependency_freeze"] = attempt_process([sys.executable, "-m", "pip", "freeze"], 60)
        if (
            result["dependency_freeze"]["exit_code"] != 0
            or result["dependency_freeze"]["timed_out"]
            or result["dependency_freeze"]["descendants_remaining"]
        ):
            writer.error("dependency-freeze")
        for mode in ("baseline", "observed"):
            arguments = [
                sys.executable,
                "-I",
                str(Path(__file__).resolve()),
                "--binding",
                json.dumps(binding, separators=(",", ":")),
                "--variant",
                mode,
            ]
            status = attempt_process(arguments, 300)
            writer.refresh()
            try:
                summary = read_summary(root, mode, binding)
            except (OSError, ValueError) as exc:
                summary = {
                    "complete": False,
                    "pytest_exit_code": None,
                    "missing_case_ids": list(EXPECTED),
                    "error_kind": type(exc).__name__,
                }
            result[mode] = {"process": status, **summary}
        for arguments in (["check", "--output-format", "full"], ["format", "--check", "--diff"]):
            command = [sys.executable, "-m", "ruff", *arguments, *RUFF_TARGETS]
            result["ruff"].append({"arguments": arguments, **attempt_process(command, 60)})
        result["complete"] = (
            all(
                result[m]["complete"]
                and not result[m]["process"]["timed_out"]
                and not result[m]["process"]["descendants_remaining"]
                and result[m]["process"]["exit_code"] in {0, 1}
                for m in ("baseline", "observed")
            )
            and not writer.errors
            and len(result["ruff"]) == 2
            and all(not r["timed_out"] and not r["descendants_remaining"] for r in result["ruff"])
        )
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        result["error_kind"] = type(exc).__name__
    finally:
        writer.refresh()
        result["errors"] = dict(writer.errors)
        if not writer.json("result.json", result, final=True):
            raise ValueError("Could not retain lifecycle diagnostic result")
    passed = result["complete"] and all(
        result[m]["pytest_exit_code"] == 0 and result[m]["process"]["exit_code"] == 0
        for m in ("baseline", "observed")
    )
    return (
        passed
        and len(result["ruff"]) == 2
        and all(
            r["exit_code"] == 0 and not r["timed_out"] and not r["descendants_remaining"]
            for r in result["ruff"]
        )
    )


def main(arguments):
    if len(arguments) not in {2, 4} or arguments[0] != "--binding":
        raise ValueError("Only the fixed lifecycle binding is accepted")
    binding = parse_binding(arguments[1])
    if hashlib.sha256(regular(Path(__file__).resolve())).hexdigest() != binding["adapter_sha256"]:
        raise ValueError("Lifecycle adapter differs from the exact reviewed source")
    if len(arguments) == 2:
        return run(binding)
    if arguments[2] != "--variant" or arguments[3] not in {"baseline", "observed"}:
        raise ValueError("Unreviewed lifecycle child mode")
    return variant(binding, arguments[3])


if __name__ == "__main__" and not main(sys.argv[1:]):
    raise SystemExit("Lifecycle diagnostics failed; original outcomes remain retained")
