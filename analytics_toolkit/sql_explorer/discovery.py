"""Find configuration paths near virtual environments without reading credentials."""

from __future__ import annotations

import ctypes
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from analytics_toolkit.sql.connection import config_path

if TYPE_CHECKING:
    from collections.abc import Iterator
    from threading import Event

_LOCAL_FILESYSTEMS = frozenset(
    {
        "ext2",
        "ext3",
        "ext4",
        "btrfs",
        "xfs",
        "zfs",
        "vfat",
        "exfat",
        "ntfs",
        "ntfs3",
        "fuseblk",
        "ufs",
        "overlay",
        "rootfs",
        "squashfs",
        "bcachefs",
        "f2fs",
    }
)
_MOUNT_ESCAPE = re.compile(r"\\([0-7]{3})")
_MOUNTINFO_MIN_FIELDS = 5
_PROGRESS_EVERY = 250


@dataclass(frozen=True)
class MountedFilesystem:
    path: Path
    local: bool


@dataclass(frozen=True)
class DiscoveryProgress:
    paths: tuple[Path, ...]
    directories: int = 0
    complete: bool = False


def _unescape_mount(value: str) -> str:
    return _MOUNT_ESCAPE.sub(lambda match: chr(int(match[1], 8)), value)


def _linux_mounts(content: str) -> tuple[MountedFilesystem, ...]:
    mounts = []
    for line in content.splitlines():
        left, separator, right = line.partition(" - ")
        fields, filesystem = left.split(), right.split()
        if separator and len(fields) >= _MOUNTINFO_MIN_FIELDS and filesystem:
            mounts.append(
                MountedFilesystem(
                    Path(_unescape_mount(fields[4])), filesystem[0] in _LOCAL_FILESYSTEMS
                )
            )
    return tuple(mounts)


def _macos_mounts(content: str) -> tuple[MountedFilesystem, ...]:
    mounts = []
    for line in content.splitlines():
        location, separator, options = line.rpartition(" (")
        _source, mounted, path = location.partition(" on ")
        if separator and mounted:
            flags = options.rstrip(")").split(", ")
            mounts.append(MountedFilesystem(Path(_unescape_mount(path)), "local" in flags))
    return tuple(mounts)


def mounted_filesystems() -> tuple[MountedFilesystem, ...]:
    """Enumerate mount boundaries, including excluded remote/pseudo mounts."""
    if sys.platform == "win32":
        kernel = ctypes.windll.kernel32
        mask = kernel.GetLogicalDrives()
        return tuple(
            MountedFilesystem(Path(drive), kernel.GetDriveTypeW(drive) in {2, 3})
            for index in range(26)
            if mask & (1 << index)
            for drive in (f"{chr(65 + index)}:\\",)
        )
    if sys.platform == "darwin":
        result = subprocess.run(
            ["/sbin/mount"], capture_output=True, text=True, check=True, timeout=5
        )
        return _macos_mounts(result.stdout)
    return _linux_mounts(Path("/proc/self/mountinfo").read_text(encoding="utf-8"))


def nearest_connections(root: Path) -> Path | None:
    for directory in config_path._iter_search_directories([root]):  # noqa: SLF001 -- shared SQL lookup.
        candidate = directory / ".connections"
        try:
            if candidate.is_file():
                return candidate.resolve()
        except OSError:
            continue
    return None


def discover_connections(cancelled: Event) -> Iterator[DiscoveryProgress]:
    """Scan mounted local disks, emitting bounded progress and canonical candidates."""
    paths: set[Path] = set()
    caller = config_path._resolve_calling_base_dir()  # noqa: SLF001 -- shared caller resolution.
    for root in (Path.cwd(), Path(sys.prefix), *((caller,) if caller else ())):
        candidate = nearest_connections(root)
        if candidate is not None:
            paths.add(candidate)
    yield DiscoveryProgress(tuple(sorted(paths)))
    mounts = mounted_filesystems()
    boundaries = {mount.path for mount in mounts}
    visited: set[tuple[int, int]] = set()
    count = 0
    for mount in mounts:
        if not mount.local:
            continue
        for current, filenames in _walk_mount(mount.path, boundaries, visited, cancelled):
            count += 1
            previous_count = len(paths)
            if "pyvenv.cfg" in filenames:
                candidate = nearest_connections(current)
                if candidate is not None:
                    paths.add(candidate)
            if len(paths) != previous_count or count % _PROGRESS_EVERY == 0:
                yield DiscoveryProgress(tuple(sorted(paths)), count)
    if not cancelled.is_set():
        yield DiscoveryProgress(tuple(sorted(paths)), count, complete=True)


def _walk_mount(
    root: Path, boundaries: set[Path], visited: set[tuple[int, int]], cancelled: Event
) -> Iterator[tuple[Path, list[str]]]:
    for directory, directories, filenames in os.walk(root, followlinks=False):
        if cancelled.is_set():
            return
        current = Path(directory)
        try:
            stat = current.stat()
        except OSError:
            directories.clear()
            continue
        identity = (stat.st_dev, stat.st_ino)
        if identity in visited:
            directories.clear()
            continue
        visited.add(identity)
        directories[:] = [
            name
            for name in directories
            if current / name not in boundaries and not (current / name).is_symlink()
        ]
        yield current, filenames
