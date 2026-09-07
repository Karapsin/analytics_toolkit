from __future__ import annotations

from pathlib import Path
from threading import Event
from types import SimpleNamespace
from typing import TYPE_CHECKING

from analytics_toolkit.sql_explorer import discovery
from analytics_toolkit.sql_explorer.discovery import MountedFilesystem

if TYPE_CHECKING:
    import pytest


def test_mount_parsers_exclude_remote_and_pseudo_filesystems() -> None:
    mounts = discovery._linux_mounts(
        "1 0 8:1 / / rw - ext4 /dev/sda1 rw\n"
        "2 1 0:1 / /proc rw - proc proc rw\n"
        "3 1 0:2 / /remote rw - nfs server:/files rw\n"
        "4 1 8:2 / /local\\040disk rw - exfat /dev/sdb1 rw\ninvalid"
    )
    assert mounts == (
        MountedFilesystem(Path("/"), True),
        MountedFilesystem(Path("/proc"), False),
        MountedFilesystem(Path("/remote"), False),
        MountedFilesystem(Path("/local disk"), True),
    )
    assert discovery._macos_mounts(
        "/dev/disk1 on / (apfs, local, journaled)\n"
        "map auto_home on /System/Volumes/Data/home (autofs, automounted)\n"
        "server:/share on /Volumes/team (nfs, nodev)\n"
        "/dev/disk2 on /Volumes/My Disk (exfat, local)\nbad line"
    ) == (
        MountedFilesystem(Path("/"), True),
        MountedFilesystem(Path("/System/Volumes/Data/home"), False),
        MountedFilesystem(Path("/Volumes/team"), False),
        MountedFilesystem(Path("/Volumes/My Disk"), True),
    )


def test_platform_mount_enumeration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(discovery.sys, "platform", "darwin")
    monkeypatch.setattr(
        discovery.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(stdout="/dev/disk1 on / (apfs, local)"),
    )
    assert discovery.mounted_filesystems() == (MountedFilesystem(Path("/"), True),)
    monkeypatch.setattr(discovery.sys, "platform", "win32")
    monkeypatch.setattr(
        discovery.ctypes,
        "windll",
        SimpleNamespace(
            kernel32=SimpleNamespace(
                GetLogicalDrives=lambda: (1 << 2) | (1 << 3),
                GetDriveTypeW=lambda drive: 3 if drive.startswith("C") else 4,
            )
        ),
        raising=False,
    )
    mounts = discovery.mounted_filesystems()
    assert [mount.local for mount in mounts] == [True, False]


def test_discovery_finds_arbitrary_venv_names_deduplicates_and_skips_mounts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    disk = tmp_path / "disk"
    first = disk / "project"
    second = disk / "nested-disk" / "another"
    remote = disk / "remote"
    for root in (first, second, remote):
        env = root / "custom-python"
        env.mkdir(parents=True)
        (env / "pyvenv.cfg").touch()
        (root / ".connections").write_text("not read during discovery", encoding="utf-8")
    (first / "another-env").mkdir()
    (first / "another-env" / "pyvenv.cfg").touch()
    (first / "loop").symlink_to(disk, target_is_directory=True)
    monkeypatch.setattr(discovery.sys, "prefix", str(tmp_path))
    monkeypatch.setattr(
        discovery,
        "mounted_filesystems",
        lambda: (
            MountedFilesystem(disk, True),
            MountedFilesystem(second.parent, True),
            MountedFilesystem(remote, False),
            MountedFilesystem(disk, True),
        ),
    )
    updates = list(discovery.discover_connections(Event()))
    assert updates[-1].complete
    assert set(updates[-1].paths) == {
        tmp_path / ".connections",
        first / ".connections",
        second / ".connections",
    }
    assert not updates[0].complete


def test_cancelled_scan_does_not_report_completion(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(discovery.sys, "prefix", str(tmp_path))
    monkeypatch.setattr(
        discovery, "mounted_filesystems", lambda: (MountedFilesystem(tmp_path, True),)
    )
    cancelled = Event()
    cancelled.set()
    assert not any(update.complete for update in discovery.discover_connections(cancelled))


def test_nearest_path_recovers_from_inaccessible_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    child = tmp_path / "inaccessible"
    original = Path.is_file

    def is_file(path: Path) -> bool:
        if path.parent == child:
            raise PermissionError
        return original(path)

    monkeypatch.setattr(Path, "is_file", is_file)
    assert discovery.nearest_connections(child) == tmp_path / ".connections"


def test_linux_mount_enumeration_reads_mountinfo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(discovery.sys, "platform", "linux")
    monkeypatch.setattr(Path, "read_text", lambda *args, **kwargs: "1 0 8:1 / / rw - ext4 disk rw")
    assert discovery.mounted_filesystems() == (MountedFilesystem(Path("/"), True),)


def test_scan_without_candidates_reports_progress_and_skips_disappearing_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(discovery.config_path, "_resolve_calling_base_dir", lambda: None)
    monkeypatch.setattr(discovery.config_path, "_iter_search_directories", lambda roots: iter(()))
    monkeypatch.setattr(
        discovery, "mounted_filesystems", lambda: (MountedFilesystem(tmp_path, True),)
    )
    monkeypatch.setattr(discovery, "_PROGRESS_EVERY", 1)
    gone = tmp_path / "gone"
    children = ["child"]
    monkeypatch.setattr(
        discovery.os,
        "walk",
        lambda *args, **kwargs: iter(
            [(str(gone), children, []), (str(tmp_path), [], ["pyvenv.cfg"])]
        ),
    )
    updates = list(discovery.discover_connections(Event()))
    assert children == []
    assert len(updates) == 3
    assert updates[-1].complete
    assert updates[-1].paths == ()
