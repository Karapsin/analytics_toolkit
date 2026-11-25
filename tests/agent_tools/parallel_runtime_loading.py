"""Parallel trusted-adapter imports retain independent dataclass module identities."""

from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

from agent_tools import agent_runtimes, local_review, sessions
from tests.agent_tools.local_review import candidate as candidate  # noqa: PLC0414
from tests.agent_tools.local_review_runtimes import adapter_base as adapter_base  # noqa: PLC0414
from tests.agent_tools.sessions import source as source  # noqa: PLC0414


def test_parallel_trusted_runtime_loads_keep_modules_registered_until_each_import_finishes(
    adapter_base, tmp_path, monkeypatch
):
    entered = Event()
    release = Event()
    names = []
    original_spec = local_review.importlib.util.spec_from_file_location

    def controlled_spec(name, location, *args, **kwargs):
        spec = original_spec(name, location, *args, **kwargs)
        if not (
            name.startswith("atk_trusted_agent_runtimes_")
            and Path(location).name.startswith("agent_runtimes-")
        ):
            return spec
        assert spec is not None
        assert spec.loader is not None
        names.append(name)
        if len(names) == 1:
            execute = spec.loader.exec_module

            def pause_after_registration(module):
                assert sys.modules[module.__name__] is module
                entered.set()
                assert release.wait(10), "First trusted runtime import was never released"
                execute(module)

            spec.loader.exec_module = pause_after_registration
        return spec

    monkeypatch.setattr(local_review.importlib.util, "spec_from_file_location", controlled_spec)
    first_job = tmp_path / "first-review-job"
    second_job = tmp_path / "second-review-job"
    first_job.mkdir()
    second_job.mkdir()
    base = sessions.load(adapter_base)["base"]
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(local_review.runtime_adapter, adapter_base, first_job, base)
        try:
            assert entered.wait(10), "First trusted runtime import did not reach its loader"
            second = executor.submit(
                local_review.runtime_adapter, adapter_base, second_job, base
            ).result(timeout=10)
        finally:
            release.set()
        try:
            initial = first.result(timeout=10)
        finally:
            assert all(name not in sys.modules for name in names)

    assert len(names) == len(set(names)) == 2
    assert initial.__name__ == names[0]
    assert second.__name__ == names[1]
    assert initial is not second
    for module in (initial, second):
        assert module.Runtime.__module__ == module.__name__
        assert module.REVIEW_TIMEOUT == agent_runtimes.REVIEW_TIMEOUT
        assert set(module.RUNTIMES) == {"codex", "claude"}
        assert module.render_mcp_json() == agent_runtimes.render_mcp_json()
        for name, runtime in module.RUNTIMES.items():
            assert isinstance(runtime, module.Runtime)
            expected = agent_runtimes.RUNTIMES[name]
            assert runtime.name == expected.name
            assert runtime.receipt_name == expected.receipt_name
            assert runtime.auth == expected.auth
