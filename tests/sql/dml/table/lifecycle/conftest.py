from __future__ import annotations

import pytest

from tests.sql._support.lifecycle import LifecycleAdapter, maintenance


@pytest.fixture
def lifecycle_adapter(monkeypatch: pytest.MonkeyPatch) -> LifecycleAdapter:
    adapter = LifecycleAdapter()
    monkeypatch.setattr(maintenance, "resolve_connection_backend", lambda value: value)
    monkeypatch.setattr(maintenance, "get_backend_adapter", lambda _backend: adapter)
    monkeypatch.setattr(maintenance, "time_print", lambda *_args, **_kwargs: None)
    return adapter
