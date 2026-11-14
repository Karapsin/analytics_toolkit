from __future__ import annotations

from types import SimpleNamespace

from tests.sql.backends.ch._support.exact_transport import native_context


def client(transport: str, *, context: bool = True):
    return SimpleNamespace(
        is_native_transport=transport == "native",
        context=native_context() if context else None,
        effective_write_timezone="UTC",
    )
