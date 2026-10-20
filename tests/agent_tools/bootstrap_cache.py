from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import url2pathname

import pytest

from agent_tools import bootstrap_env
from tests.agent_tools._support.bootstrap import Runner, manifest, prepare, project


@pytest.fixture
def cache(tmp_path, monkeypatch):
    location = tmp_path / "cache"
    monkeypatch.setenv("ATK_BOOTSTRAP_CACHE_DIR", str(location))
    monkeypatch.delenv("ATK_BOOTSTRAP_REFRESH", raising=False)
    return location


def test_new_clones_share_wheels_and_use_local_only_installs(tmp_path, cache):
    runner = Runner()
    first = project(tmp_path / "first")
    second = project(tmp_path / "second")
    (second / "unrelated.py").write_text("changed source\n")
    results, cold = prepare(first, runner)
    repeated, warm = prepare(second, runner)
    assert all(item["ok"] for item in [*results, *repeated])
    assert cold["key"] == warm["key"]
    assert cold["reused"] is False
    assert warm["reused"] is True
    assert len(runner.downloads) == 1
    installs = [command for command in runner.commands if "install" in command["args"]]
    assert installs
    for command in installs:
        assert command["env"]["PIP_NO_INDEX"] == "1"
        assert Path(url2pathname(urlsplit(command["env"]["PIP_FIND_LINKS"]).path)).is_dir()
        assert command["env"]["PIP_EXTRA_INDEX_URL"] == ""
    assert "--wheel-dir" in runner.downloads[0]["args"]
    assert "wheel" in runner.downloads[0]["args"]


@pytest.mark.parametrize("name", ["agent_tools/requirements-mcp.txt", "pyproject.toml", "tox.ini"])
def test_dependency_inputs_invalidate_cache_but_source_changes_do_not(tmp_path, cache, name):
    root = project(tmp_path / "root")
    runner = Runner()
    _, first = prepare(root, runner)
    with (root / name).open("a") as output:
        output.write("\n# changed\n")
    _, changed = prepare(root, runner)
    assert first["key"] != changed["key"]
    assert len(runner.downloads) == 2


def test_runtime_and_profile_are_part_of_cache_identity(tmp_path, cache, monkeypatch):
    root = project(tmp_path / "root")
    original = bootstrap_env.fingerprint(root, project=True)
    assert original != bootstrap_env.fingerprint(root, project=False)
    monkeypatch.setattr(bootstrap_env.sysconfig, "get_platform", lambda: "another-platform")
    assert original != bootstrap_env.fingerprint(root, project=True)
    monkeypatch.setattr(bootstrap_env.sys.implementation, "cache_tag", "different-abi")
    assert original != bootstrap_env.fingerprint(root, project=True)


@pytest.mark.parametrize(
    "damage", ["wheel", "missing", "json", "type", "generation", "files", "key"]
)
def test_corrupted_or_incomplete_cache_is_rebuilt(tmp_path, cache, damage):
    root = project(tmp_path / "root")
    runner = Runner()
    prepare(root, runner)
    path, receipt = manifest(cache)
    wheels = path.parent / receipt["generation"]
    if damage == "wheel":
        next(wheels.iterdir()).write_bytes(b"broken")
    elif damage == "missing":
        next(wheels.iterdir()).unlink()
    else:
        replacements = {
            "json": "broken",
            "type": "[]",
            "generation": json.dumps({**receipt, "generation": "../outside"}),
            "files": json.dumps({**receipt, "files": ["not-a-manifest"]}),
            "key": json.dumps({**receipt, "key": "wrong"}),
        }
        path.write_text(replacements[damage])
    results, state = prepare(root, runner)
    assert all(item["ok"] for item in results)
    assert state["reused"] is False
    assert len(runner.downloads) == 2


@pytest.mark.parametrize(
    "failure",
    [
        "Download/build dependency wheels",
        "Verify session environment imports",
        "Verify installed dependency compatibility",
    ],
)
def test_failed_refresh_preserves_previous_cache(tmp_path, cache, monkeypatch, failure):
    root = project(tmp_path / "root")
    runner = Runner()
    prepare(root, runner)
    path, old = manifest(cache)
    monkeypatch.setenv("ATK_BOOTSTRAP_REFRESH", "1")
    runner.fail = failure
    results, _ = prepare(root, runner)
    assert results[-1]["ok"] is False
    assert json.loads(path.read_text()) == old
    assert [item.name for item in path.parent.iterdir() if item.is_dir()] == [old["generation"]]
    runner.fail = None
    prepare(root, runner)
    assert json.loads(path.read_text())["generation"] != old["generation"]
    assert not (path.parent / old["generation"]).exists()


def test_interruption_does_not_publish_partial_wheels(tmp_path, cache):
    runner = Runner()
    runner.interrupt = True
    with pytest.raises(KeyboardInterrupt):
        prepare(project(tmp_path / "root"), runner)
    assert not list(cache.glob("*/ready.json"))
    assert not [item for item in cache.glob("*/*") if item.is_dir()]


def test_venv_failure_stops_before_download(tmp_path, cache):
    runner = Runner()
    runner.fail = "python -m venv .venv"
    results, _ = prepare(project(tmp_path / "root"), runner)
    assert results[-1]["ok"] is False
    assert not runner.downloads
    assert not list(cache.glob("*/ready.json"))


def test_mcp_only_profile_does_not_download_project_dependencies(tmp_path, cache):
    root = project(tmp_path / "root")
    (root / "pyproject.toml").unlink()
    runner = Runner()
    results, _ = prepare(root, runner, full=False)
    assert all(item["ok"] for item in results)
    assert "pytest" not in runner.downloads[0]["args"]


def test_concurrent_clones_build_one_cache(tmp_path, cache, monkeypatch):
    entered, waiting, release = threading.Event(), threading.Event(), threading.Event()
    first, second = project(tmp_path / "first"), project(tmp_path / "second")
    runner = Runner()

    def paused(root, command):
        if command.get("network"):
            entered.set()
            assert release.wait(10)
        return runner(root, command)

    def observe(message):
        if threading.current_thread().name.endswith("_1") and "lock" in message:
            waiting.set()

    monkeypatch.setattr(bootstrap_env.process, "progress", observe)
    with ThreadPoolExecutor(max_workers=2) as pool:
        try:
            one = pool.submit(prepare, first, paused)
            assert entered.wait(10)
            two = pool.submit(prepare, second, paused)
            assert waiting.wait(10)
        finally:
            release.set()
        assert one.result(timeout=10)[1]["reused"] is False
        assert two.result(timeout=10)[1]["reused"] is True
    assert len(runner.downloads) == 1


def test_cache_requires_explicit_location(tmp_path, monkeypatch):
    monkeypatch.delenv("ATK_BOOTSTRAP_CACHE_DIR", raising=False)
    assert bootstrap_env.cache_root() is None
    with pytest.raises(RuntimeError, match="directory"):
        prepare(project(tmp_path / "root"), Runner())
