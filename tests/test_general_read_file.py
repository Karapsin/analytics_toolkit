from __future__ import annotations

import __main__
from collections import namedtuple
from pathlib import Path

from analytics_toolkit.general import here
from analytics_toolkit.general.read_file import _resolve_base_dir


def test_here_prefers_main_module_directory_for_new_paths(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(__main__, "__file__", str(Path(__file__).resolve()), raising=False)

    resolved = here("new_output.xlsx")

    assert resolved == str(Path(__file__).resolve().parent / "new_output.xlsx")


def test_here_falls_back_to_cwd_without_main_file(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delattr(__main__, "__file__", raising=False)

    resolved = here("new_output.xlsx")

    assert resolved == str(tmp_path / "new_output.xlsx")


def test_here_ignores_ipykernel_main_file_and_uses_cwd(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        __main__,
        "__file__",
        "/private/var/folders/vq/zns5cfbd6zd64jw8hfgzzczr0000gq/T/ipykernel_99706/123.py",
        raising=False,
    )

    resolved = here("new_output.xlsx")

    assert resolved == str(tmp_path / "new_output.xlsx")


def test_resolve_base_dir_uses_first_real_caller_file(monkeypatch) -> None:
    FrameInfo = namedtuple("FrameInfo", ["filename"])
    fake_stack = [
        FrameInfo(filename="/Users/test/project/utils_dev/analytics_toolkit/general/read_file.py"),
        FrameInfo(filename="/private/var/folders/vq/zns5cfbd6zd64jw8hfgzzczr0000gq/T/ipykernel_99706/123.py"),
        FrameInfo(filename="/opt/homebrew/Cellar/python@3.14/3.14.3_1/Frameworks/Python.framework/Versions/3.14/lib/python3.14/asyncio/events.py"),
        FrameInfo(filename="/Users/test/project/tickets/april_2026/MAL-3657/compute_metrics.py"),
    ]
    monkeypatch.delattr(__main__, "__file__", raising=False)
    monkeypatch.setattr("analytics_toolkit.general.read_file.inspect.stack", lambda: fake_stack)

    resolved = _resolve_base_dir()

    assert resolved == Path("/Users/test/project/tickets/april_2026/MAL-3657")
