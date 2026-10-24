from __future__ import annotations

from tests.agent_tools._support.mcp import (
    Path,
    _command_result,
    _init_git_repo,
    _write_minimal_repo_files,
    json,
    mcp_server,
    pytest,
)


@pytest.fixture
def checked_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = _write_minimal_repo_files(tmp_path / "project")
    _init_git_repo(root)
    tools = {"identity": "original-tools"}
    calls = []
    actual_run = mcp_server._run_command

    def git(root_path, args):
        return actual_run(
            root_path, {"display": "git " + " ".join(args), "args": ["git", *args], "env": {}}
        )

    def run(root_path, command):
        calls.append(command["stage"])
        return _command_result(command["display"], "passed")

    monkeypatch.setattr(
        mcp_server, "_precommit_toolchain_fingerprint", lambda root: tools["identity"]
    )
    monkeypatch.setattr(mcp_server, "_run_git", git)
    monkeypatch.setattr(mcp_server, "_run_command", run)
    return root, tools, calls


def _blocked(result):
    assert result["ok"] is False
    assert result["blockers"][0]["code"] == "context_changed"


@pytest.mark.parametrize("path", ["README.md", "new_source.py"])
def test_source_changed_during_successful_stage_is_not_certified(
    checked_project,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
):
    root, _tools, calls = checked_project

    def changed(root_path, command):
        calls.append(command["stage"])
        (root / path).write_text("Changed while the command was running\n")
        return _command_result(command["display"], "passed")

    monkeypatch.setattr(mcp_server, "_run_command", changed)
    result = mcp_server.run_checks(level="precommit", root=str(root))

    _blocked(result)
    assert calls == ["static"]
    assert result["result"]["stages"][0]["status"] == "invalidated"
    assert not (root / mcp_server.CHECK_STATE_FILE).exists()
    assert mcp_server._load_precommit_stage_state(root)["stages"] == {}
    assert (root / path).read_text() == "Changed while the command was running\n"


def test_prior_stages_keep_original_identity_and_changed_tree_reruns_all_stages(
    checked_project,
    monkeypatch: pytest.MonkeyPatch,
):
    root, _tools, calls = checked_project
    initial = mcp_server._working_tree_fingerprint(root)
    changed_once = False

    def run(root_path, command):
        nonlocal changed_once
        calls.append(command["stage"])
        if command["stage"] == "coverage" and not changed_once:
            changed_once = True
            (root / "README.md").write_text("New source\n")
        return _command_result(command["display"], "passed")

    monkeypatch.setattr(mcp_server, "_run_command", run)
    _blocked(mcp_server.run_checks(level="precommit", root=str(root)))
    state = mcp_server._load_precommit_stage_state(root)["stages"]
    assert set(state) == {"static"}
    assert state["static"]["fingerprint"] == initial
    assert calls == ["static", "coverage"]
    calls.clear()
    result = mcp_server.run_checks(level="precommit", root=str(root))
    assert result["ok"] is True
    assert calls == ["static", "coverage", "artifacts"]
    assert result["result"]["reused_stage_count"] == 0
    current = mcp_server._working_tree_fingerprint(root)
    assert current != initial
    assert {
        row["fingerprint"]
        for row in mcp_server._load_precommit_stage_state(root)["stages"].values()
    } == {current}
    assert mcp_server._verify_precommit_success(root)["ok"] is True


def test_toolchain_change_during_a_stage_stops_before_receipt_or_next_command(
    checked_project,
    monkeypatch: pytest.MonkeyPatch,
):
    root, tools, calls = checked_project

    def changed(root_path, command):
        calls.append(command["stage"])
        tools["identity"] = "different-tools"
        return _command_result(command["display"], "passed")

    monkeypatch.setattr(mcp_server, "_run_command", changed)
    _blocked(mcp_server.run_checks(level="precommit", root=str(root)))
    assert calls == ["static"]
    assert not (root / mcp_server.CHECK_STATE_FILE).exists()
    assert mcp_server._load_precommit_stage_state(root)["stages"] == {}


def test_edit_between_stages_prevents_launching_the_next_command(
    checked_project,
    monkeypatch: pytest.MonkeyPatch,
):
    root, _tools, calls = checked_project
    original = mcp_server._record_precommit_stage_success

    def persisted(root_path, stage, receipt):
        original(root_path, stage, receipt)
        (root / "README.md").write_text("Edited after completed stage\n")

    monkeypatch.setattr(mcp_server, "_record_precommit_stage_success", persisted)
    _blocked(mcp_server.run_checks(level="precommit", root=str(root)))
    assert calls == ["static"]
    assert not (root / mcp_server.CHECK_STATE_FILE).exists()


def test_last_reused_stage_cannot_certify_a_new_tree(
    checked_project,
    monkeypatch: pytest.MonkeyPatch,
):
    root, _tools, calls = checked_project
    assert mcp_server.run_checks(level="precommit", root=str(root))["ok"] is True
    original_receipt = (root / mcp_server.CHECK_STATE_FILE).read_bytes()
    original = mcp_server._reusable_precommit_stage
    calls.clear()

    def reuse(context, stage, command):
        receipt = original(context, stage, command)
        if stage == "artifacts":
            assert receipt is not None
            (root / "README.md").write_text("Changed after final reuse lookup\n")
        return receipt

    monkeypatch.setattr(mcp_server, "_reusable_precommit_stage", reuse)
    _blocked(mcp_server.run_checks(level="precommit", root=str(root)))
    assert calls == []
    assert (root / mcp_server.CHECK_STATE_FILE).read_bytes() == original_receipt
    assert mcp_server._verify_precommit_success(root)["ok"] is False


def test_final_completion_rechecks_toolchain_after_all_stage_results(
    checked_project,
    monkeypatch: pytest.MonkeyPatch,
):
    root, tools, calls = checked_project
    original = mcp_server._execute_check_commands

    def completed(*args):
        result = original(*args)
        tools["identity"] = "different-tools"
        return result

    monkeypatch.setattr(mcp_server, "_execute_check_commands", completed)
    _blocked(mcp_server.run_checks(level="precommit", root=str(root)))
    assert calls == ["static", "coverage", "artifacts"]
    assert not (root / mcp_server.CHECK_STATE_FILE).exists()
    assert {
        row["toolchain_fingerprint"]
        for row in mcp_server._load_precommit_stage_state(root)["stages"].values()
    } == {"original-tools"}


@pytest.mark.parametrize("change", ["tools", "legacy"])
def test_commit_readiness_rejects_different_toolchain_and_legacy_receipts(
    checked_project,
    change: str,
):
    root, tools, _calls = checked_project
    assert mcp_server.run_checks(level="precommit", root=str(root))["ok"] is True
    receipt = root / mcp_server.CHECK_STATE_FILE
    if change == "tools":
        tools["identity"] = "different-tools"
    else:
        value = json.loads(receipt.read_text())
        value.pop("format_version")
        receipt.write_text(json.dumps(value))
    assert mcp_server._verify_precommit_success(root)["ok"] is False
