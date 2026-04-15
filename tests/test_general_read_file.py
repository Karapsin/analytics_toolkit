from __future__ import annotations

from collections import namedtuple
from pathlib import Path

from analytics_toolkit.general import here
from analytics_toolkit.general.read_file import _resolve_caller_path


def test_here_prefers_caller_directory_for_new_paths(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)

    resolved = here("new_output.xlsx")

    assert resolved == str(Path(__file__).resolve().parent / "new_output.xlsx")


def test_resolve_caller_path_ignores_ipython_frames_and_falls_back(monkeypatch, tmp_path: Path) -> None:
    FrameInfo = namedtuple("FrameInfo", ["filename"])
    fake_stack = [
        FrameInfo(filename="/Users/test/project/utils_dev/analytics_toolkit/general/read_file.py"),
        FrameInfo(filename="/Applications/Positron.app/Contents/Resources/app/extensions/positron-python/python_files/lib/ipykernel/py3/IPython/core/interactiveshell.py"),
        FrameInfo(filename="<stdin>"),
    ]
    monkeypatch.setattr("analytics_toolkit.general.read_file.inspect.stack", lambda: fake_stack)

    resolved = _resolve_caller_path("new_output.xlsx")

    assert resolved is None
