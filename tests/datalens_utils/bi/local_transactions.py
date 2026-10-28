"""Abrupt process exits and competing edits never leave an accepted partial recipe."""

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from analytics_toolkit.datalens_utils import local_transactions as transactions
from analytics_toolkit.datalens_utils.errors import DataLensConfigurationError, DataLensUtilsError
from analytics_toolkit.datalens_utils.session import current_session

from tests._support.paths import REPO_ROOT


def crash(project, *, committed=False):
    """A real child dies after a durable write, bypassing Python exception rollback."""
    script = """
import os, sys
from types import SimpleNamespace
from analytics_toolkit.datalens_utils import local_transactions as tx
from analytics_toolkit.datalens_utils.session import ProjectPaths
state = SimpleNamespace(paths=ProjectPaths(sys.argv[1], sys.argv[2]), destination_snapshot=None)
original = tx._save if sys.argv[3] == "commit" else tx._write
def die(*args):
    original(*args)
    if sys.argv[3] != "commit" or args[1]["state"] == "committed":
        os._exit(23)
if sys.argv[3] == "commit":
    tx._save = die
else:
    tx._write = die
tx.replace({"configs/runtime.json": b"invalid partial config", "assets/new.txt": b"new"}, state)
"""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(project.paths.project_root),
            str(project.paths.runtime_root),
            "commit" if committed else "prepare",
        ],
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 23, result.stderr


def test_restart_recovers_before_parsing_partial_configuration(bi_project):
    root = bi_project.paths.project_root
    before = transactions.snapshot(root)
    crash(bi_project)
    assert (root / "configs/runtime.json").read_bytes() == b"invalid partial config"
    with bi_project.session() as state:
        assert any("Recovered interrupted" in message for message in state.messages)
    assert transactions.snapshot(root) == before
    assert not transactions._journal(bi_project.paths).exists()


def test_restart_retains_fully_committed_transaction(bi_project):
    crash(bi_project, committed=True)
    assert transactions.recover(bi_project.paths)
    root = bi_project.paths.project_root
    assert (root / "configs/runtime.json").read_bytes() == b"invalid partial config"
    assert (root / "assets/new.txt").read_bytes() == b"new"
    assert not transactions.recover(bi_project.paths)


def test_recovery_refuses_user_edit_without_rolling_back_other_files(bi_project):
    crash(bi_project)
    root = bi_project.paths.project_root
    path = root / "assets/new.txt"
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(b"user edit")
    with pytest.raises(DataLensUtilsError, match="recovery conflict"):
        transactions.recover(bi_project.paths)
    assert path.read_bytes() == b"user edit"
    assert (root / "configs/runtime.json").read_bytes() == b"invalid partial config"
    assert transactions._journal(bi_project.paths).is_file()


def test_destination_fingerprint_refuses_edit_during_operation(bi_project):
    root = bi_project.paths.project_root
    (root / "new-local-file").write_text("keep me")
    with pytest.raises(DataLensUtilsError, match="destination changed during the operation"):
        transactions.replace({"configs/UI/tabs.json": b"{}"}, current_session())
    assert (root / "new-local-file").read_text() == "keep me"
    assert "main" in json.loads((root / "configs/UI/tabs.json").read_text())


@pytest.mark.parametrize("name", [".connections", "../escape", "/absolute", "linked"])
def test_replacement_refuses_private_or_escaping_paths(bi_project, name):
    root = bi_project.paths.project_root
    (root / ".connections").write_text("private")
    (root / "linked").symlink_to(root / ".connections")
    with bi_project.session(), pytest.raises(DataLensConfigurationError, match="escapes"):
        transactions.replace({name: b"new"}, current_session())
    assert (root / ".connections").read_text() == "private"


def test_snapshot_never_opens_credentials_or_follows_links(bi_project, monkeypatch):
    root = bi_project.paths.project_root
    (root / ".connections").write_text("private")
    (root / "link").symlink_to(root / ".connections")
    (root / "directory").symlink_to(bi_project.paths.runtime_root, target_is_directory=True)
    read = Path.read_bytes

    def guarded(path):
        assert path.name != ".connections"
        assert not path.is_symlink()
        return read(path)

    monkeypatch.setattr(Path, "read_bytes", guarded)
    values = transactions.snapshot(root)
    assert ".connections" not in values
    assert values["link"].startswith("link:")
    assert values["directory"].startswith("link:")


@pytest.mark.parametrize("change", ["version", "project_root", "state", "files", "base64", "json"])
def test_invalid_journal_is_retained(bi_project, change):
    crash(bi_project)
    journal = transactions._journal(bi_project.paths)
    value = json.loads(journal.read_text())
    if change == "base64":
        value["files"]["configs/runtime.json"]["before"] = "!not-base64!"
    else:
        value[change] = "invalid"
    journal.write_text("{" if change == "json" else json.dumps(value))
    before = journal.read_bytes()
    with pytest.raises(DataLensConfigurationError, match="Invalid local recovery journal"):
        transactions.recover(bi_project.paths)
    assert journal.read_bytes() == before


def test_another_transaction_lock_is_rejected(bi_project):
    journal = transactions._journal(bi_project.paths)
    with transactions._locked(journal):  # noqa: SIM117 - Keep portable Python 3.8 syntax.
        with pytest.raises(DataLensUtilsError, match="Another local"), transactions._locked(
            journal
        ):
            pytest.fail("concurrent lock was acquired")


def test_windows_lock_is_reusable_without_growing_file(bi_project, monkeypatch):
    calls = []
    monkeypatch.setattr(transactions, "_WINDOWS", True)
    monkeypatch.setattr(
        transactions.importlib,
        "import_module",
        lambda _: SimpleNamespace(
            locking=lambda descriptor, mode, count: calls.append((mode, count)),
            LK_NBLCK=1,
            LK_UNLCK=2,
        ),
    )
    journal = transactions._journal(bi_project.paths)
    for _ in range(2):
        with transactions._locked(journal):
            pass
    assert calls == [(1, 1), (2, 1), (1, 1), (2, 1)]
    assert journal.with_suffix(".lock").stat().st_size == 1


def test_partial_write_failure_restores_deleted_and_replaced_files(bi_project, monkeypatch):
    root = bi_project.paths.project_root
    before = transactions.snapshot(root)
    write = transactions._write

    def fail(path, data):
        if path.name == "fail.txt":
            message = "disk full"
            raise OSError(message)
        write(path, data)

    monkeypatch.setattr(transactions, "_write", fail)
    with pytest.raises(OSError, match="disk full"):
        transactions.replace(
            {"assets/new.txt": b"new", "configs/UI/tabs.json": b"{}", "fail.txt": b"fail"},
            current_session(),
        )
    assert transactions.snapshot(root) == before
    assert not transactions._journal(bi_project.paths).exists()


def test_failed_rollback_remains_recoverable_after_io_is_restored(bi_project, monkeypatch):
    before = transactions.snapshot(bi_project.paths.project_root)
    write = transactions._write
    calls = []

    def fail_after_first(path, data):
        calls.append(path)
        if len(calls) > 1:
            message = "filesystem unavailable"
            raise OSError(message)
        write(path, data)

    monkeypatch.setattr(transactions, "_write", fail_after_first)
    with pytest.raises(OSError, match="filesystem unavailable"):
        transactions.replace({"configs/UI/tabs.json": b"{}", "new.txt": b"new"}, current_session())
    assert transactions._journal(bi_project.paths).is_file()
    monkeypatch.setattr(transactions, "_write", write)
    assert transactions.recover(bi_project.paths)
    assert transactions.snapshot(bi_project.paths.project_root) == before


def test_completed_rollback_marker_is_safe_to_clean_up(bi_project):
    journal = transactions._journal(bi_project.paths)
    before = transactions.snapshot(bi_project.paths.project_root)
    transactions._save(
        journal,
        {
            "version": 1,
            "project_root": str(bi_project.paths.project_root),
            "state": "rolled_back",
            "files": {},
        },
    )
    assert transactions.recover(bi_project.paths)
    assert transactions.snapshot(bi_project.paths.project_root) == before


@pytest.mark.parametrize("when", ["before", "during"])
def test_racing_editor_keeps_both_edit_and_recovery_journal(bi_project, monkeypatch, when):
    root = bi_project.paths.project_root
    target = root / "configs/UI/tabs.json"
    save, write = transactions._save, transactions._write

    def edit_on_save(path, value):
        save(path, value)
        if when == "before" and value["state"] == "prepared":
            target.write_bytes(b"editor")

    def edit_on_write(path, data):
        write(path, data)
        if when == "during":
            target.write_bytes(b"editor")

    monkeypatch.setattr(transactions, "_save", edit_on_save)
    monkeypatch.setattr(transactions, "_write", edit_on_write)
    with pytest.raises(DataLensUtilsError, match="recovery conflict"):
        transactions.replace({"configs/UI/tabs.json": b"{}"}, current_session())
    assert target.read_bytes() == b"editor"
    assert transactions._journal(bi_project.paths).is_file()
