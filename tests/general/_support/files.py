from __future__ import annotations

import importlib
from collections import namedtuple
from pathlib import Path
from types import SimpleNamespace

import pytest
from analytics_toolkit import general
from analytics_toolkit.general import from_here, here, read_file_here, write_file
from analytics_toolkit.general.read_file import (
    _find_unique_recursive_match,
    _looks_like_stdlib_path,
    _resolve_base_dir,
    _resolve_positron_editor_dir,
    read_file,
)
from analytics_toolkit.sql.connection.errors import InvalidSqlInputError

import __main__

FrameInfo = namedtuple("FrameInfo", ["filename"])

READ_FILE_MODULE = importlib.import_module("analytics_toolkit.general.read_file")

RUNTIME_STACK = [
    FrameInfo(filename="/Users/test/project/utils_dev/analytics_toolkit/general/read_file.py"),
    FrameInfo(
        filename="/private/var/folders/vq/zns5cfbd6zd64jw8hfgzzczr0000gq/T/ipykernel_99706/123.py"
    ),
    FrameInfo(
        filename="/Users/test/.venv/lib/python3.11/site-packages/IPython/core/interactiveshell.py"
    ),
    FrameInfo(
        filename="/opt/homebrew/Cellar/python@3.14/3.14.3_1/Frameworks/Python.framework/Versions/3.14/lib/python3.14/asyncio/events.py"
    ),
]


def _mock_stack(monkeypatch, frames: list[FrameInfo]) -> None:
    monkeypatch.setattr("analytics_toolkit.general.read_file.inspect.stack", lambda: frames)


def _mock_positron_parent(monkeypatch, parent: object) -> None:
    class FakeKernel:
        def get_parent(self, channel: str) -> object:
            assert channel == "shell"
            return parent

    fake_ipython = SimpleNamespace(
        get_ipython=lambda: SimpleNamespace(kernel=FakeKernel()),
    )
    monkeypatch.setattr(
        READ_FILE_MODULE,
        "import_module",
        lambda name: fake_ipython if name == "IPython" else importlib.import_module(name),
    )


def _positron_parent(uri: object) -> dict[str, object]:
    return {
        "content": {
            "positron": {
                "code_location": {
                    "uri": uri,
                },
            },
        },
    }


__all__ = [
    "READ_FILE_MODULE",
    "RUNTIME_STACK",
    "FrameInfo",
    "InvalidSqlInputError",
    "Path",
    "SimpleNamespace",
    "__main__",
    "_find_unique_recursive_match",
    "_looks_like_stdlib_path",
    "_mock_positron_parent",
    "_mock_stack",
    "_positron_parent",
    "_resolve_base_dir",
    "_resolve_positron_editor_dir",
    "from_here",
    "general",
    "here",
    "importlib",
    "namedtuple",
    "pytest",
    "read_file",
    "read_file_here",
    "write_file",
]
