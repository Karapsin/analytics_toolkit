from __future__ import annotations

import json

import pytest

from tests.repository._support.capture_native import rehearsal_runtime, source_checkout
from tests.repository._support.capture_profile import D


def test_actual_composed_source_preflight_and_main_move_before_execution(tmp_path):
    receiver, value, env, api = source_checkout(tmp_path)
    report = receiver.preflight(env, api, now=100)
    assert report["ok"] is True
    assert report["source_inventory"]
    api.moves = True
    with pytest.raises(ValueError, match="main/source tag"):
        receiver.preflight(env, api, now=100)
    api.moves = False
    value["authorization"]["fixture_manifest_sha256"] = "f" * 64
    env["INPUT_ENVELOPE"] = D.canonical(value).decode()
    # Keep actual authenticated App delivery aligned with this synthetic
    # negative request, so it reaches the real fixture predicate.
    original = api.request

    def negative(path):
        status, raw, facts = original(path)
        if path == "issues/comments/77":
            comment = D.parse(raw)
            comment["body"] = receiver.monitor_body(value)
            raw = D.canonical(comment)
        return status, raw, facts

    with pytest.raises(ValueError, match="fixture/recipe"):
        receiver.preflight(env, type("API", (), {"request": staticmethod(negative)})(), now=100)


@pytest.mark.parametrize("fault", [None, "timeout", "cleanup", "image", "unsafe-logs"])
def test_actual_reviewed_sandbox_and_complete_output_wire_with_admin_fakes(
    tmp_path, monkeypatch, fault
):
    receiver, _report, runner, root, admin, isolation_module = rehearsal_runtime(
        tmp_path, monkeypatch, fault
    )
    if fault:
        with pytest.raises(receiver.D.Pending, match="sentinel failed"):
            receiver.run_fixed_rehearsal(root)
        assert not (root / "rehearsal-result.json").exists()
        failure = json.loads((root / "rehearsal-failure.json").read_text())
        assert failure["error_type"] is not None or failure["streams"]["ok"] is False
    else:
        result = receiver.run_fixed_rehearsal(root)
        assert result["guard_result"] == "pass"
        assert result["sentinel_executed"] is True
        assert result["visual_approval"] is result["prerequisite_satisfied"] is False
        index = D.parse((root / "raw-index.json").read_bytes())
        assert result["raw_index_sha256"] == D.digest((root / "raw-index.json").read_bytes())
        assert index["cleanup_complete"] is True
        assert index["streams"]["outputs_complete"] is True
    if fault == "unsafe-logs":
        assert failure["streams"]["logs_safe"] is False
        assert failure["streams"]["readers_stopped"] is False
        assert not (root / "streams").exists()
        assert not (root / "fixture").exists()
        assert not list(root.rglob("*.bin"))
        private = list(runner.glob("capture-private-execution-*"))
        assert len(private) == 1
        assert (private[0] / "streams").is_dir()
        isolation_module.UNSAFE_LOG_WRITER.clear()
        return
    if fault == "image":
        assert not any(args[:2] == ["docker", "create"] for args in admin.commands)
        assert admin.starts == 0
        assert failure["immutable_image"] == "sha256:" + "7" * 64
        assert failure["cleanup_complete"] is True
        assert any(args[:3] == ["docker", "network", "rm"] for args in admin.commands)
        return
    create = next(args for args in admin.commands if args[:2] == ["docker", "create"])
    assert create[create.index("--network") + 1] == "none"
    assert "--read-only" in create
    assert "--cap-drop" in create
    assert create[-4:-1] == ["python", "-I", "/work/sentinel.py"]
    assert not any("GH_TOKEN" in arg for arg in create)
