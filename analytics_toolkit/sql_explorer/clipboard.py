"""Clipboard transport that works when the Explorer is reached over SSH."""

from __future__ import annotations

import base64
import sys
from contextlib import suppress
from typing import Callable, Protocol


class _Writer(Protocol):
    def write(self, value: str) -> object: ...

    def flush(self) -> object: ...


def osc52_sequence(value: str, *, clipboard: str = "c") -> str:
    payload = base64.b64encode(value.encode("utf-8")).decode("ascii")
    return f"\x1b]52;{clipboard};{payload}\x07"


class TerminalClipboard:
    def __init__(
        self,
        *,
        writer: Callable[[str], object] | _Writer | None = None,
        flush: Callable[[], object] | None = None,
    ) -> None:
        self._writer = writer
        self._flush = flush
        self._fallback = ""

    @property
    def fallback(self) -> str:
        return self._fallback

    def copy(self, value: str) -> None:
        target = self._writer
        if target is None and sys.stdout.isatty():
            target = sys.stdout
        if target is not None:
            with suppress(OSError, UnicodeError):
                write = target if callable(target) else target.write
                write(osc52_sequence(value))
                flush = self._flush
                if flush is None:
                    owner = getattr(write, "__self__", None)
                    flush = getattr(owner, "flush", None)
                if flush is not None:
                    flush()
        self._fallback = value

    def paste_fallback(self) -> str:
        return self._fallback


__all__ = ["TerminalClipboard", "osc52_sequence"]
