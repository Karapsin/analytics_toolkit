from __future__ import annotations

from tests.agent_tools._support.mcp import (
    Path,
    _write_docs_project,
    mcp_server,
    pytest,
)


def test_agent_docs_cleanup_removed_direct_docs_assistant_workflow() -> None:
    agents = (mcp_server.REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    development = (mcp_server.REPO_ROOT / "agent_docs" / "development.md").read_text(
        encoding="utf-8"
    )
    agent_tools_readme = (mcp_server.REPO_ROOT / "agent_tools" / "README.md").read_text(
        encoding="utf-8"
    )

    assert "search-docs" not in agents
    assert "ask-docs" not in agents
    assert "route-context" not in agents
    assert "repo-health" not in agents
    assert "python agent_tools/docs_assistant.py" not in agents
    assert "tests/test_sql_connection_config.py tests/test_sql_retries.py" not in development
    assert "Use `docs_assistant.py` for local documentation retrieval" not in agent_tools_readme


def test_change_impact_reports_sql_contract_and_architecture_headroom() -> None:
    result = mcp_server.change_impact(
        "add an option to sql.read",
        module="sql",
        symbols=["sql.read"],
        root=str(mcp_server.REPO_ROOT),
    )

    assert result["ok"] is True
    assert "agent_docs/development.md" in result["result"]["required_instruction_files"]
    contract = result["result"]["public_contracts"][0]
    output_type = next(item for item in contract["parameters"] if item["name"] == "output_type")
    assert output_type["pointer"] == "/exports/read/parameters/output_type"
    assert output_type["manifest_status"] == "aligned"
    budgets = {item["path"]: item for item in result["result"]["architecture"]["modules"]}
    assert budgets["analytics_toolkit/sql/backends/base.py"]["remaining_lines"] == 2
    assert "docs/modules/sql/functions/read.md" in result["result"]["documentation_paths"]
    assert result["telemetry"]["response_bytes"] < 8_000


def test_docs_default_index_dir_is_resolved_from_repo_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _write_docs_project(tmp_path / "project")
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.setattr(mcp_server, "REPO_ROOT", root)
    monkeypatch.chdir(outside)
    mcp_server.docs_assistant.build_docs_index(root=root, index_dir=root / ".rag_index")

    result = mcp_server.docs("docs MCP workflow", mode="search", top_k=1)

    assert result["ok"] is True
    assert "index_dir" not in result["input"]
    assert "resolved_index_dir" not in result["input"]
    assert result["result"]["results"][0]["citation"].startswith("agent_tools/README.md:L")


def test_docs_search_and_ask_modes(tmp_path: Path) -> None:
    root = _write_docs_project(tmp_path / "project")
    index_dir = tmp_path / "rag-index"
    mcp_server.docs_assistant.build_docs_index(root=root, index_dir=index_dir)

    search_result = mcp_server.docs(
        "docs MCP workflow",
        mode="search",
        top_k=1,
        index_dir=str(index_dir),
    )
    ask_result = mcp_server.docs(
        "docs MCP workflow",
        mode="ask",
        top_k=1,
        index_dir=str(index_dir),
    )

    assert search_result["ok"] is True
    assert search_result["tool"] == "docs"
    assert set(search_result["result"]["results"][0]) == {
        "citation",
        "heading",
        "snippet",
    }
    assert ask_result["result"]["answer"].startswith("Most relevant passages")
    assert ask_result["result"]["citations"][0].startswith("agent_tools/README.md:L")
    assert "results" not in ask_result["result"]
    assert search_result["telemetry"]["response_bytes"] <= 3_500


def test_docs_summary_deduplicates_results_and_reports_counts(tmp_path: Path) -> None:
    root = _write_docs_project(tmp_path / "project")
    index_dir = tmp_path / "rag-index"
    mcp_server.docs_assistant.build_docs_index(root=root, index_dir=index_dir)

    result = mcp_server.docs(
        "docs MCP workflow",
        top_k=3,
        index_dir=str(index_dir),
    )

    keys = [(item["citation"], item["heading"]) for item in result["result"]["results"]]
    assert len(keys) == len(set(keys))
    assert result["result"]["returned_count"] == len(keys)
    assert result["result"]["total_count"] >= len(keys)
