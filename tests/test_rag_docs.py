from __future__ import annotations

import json
import os
import sys
import types
from pathlib import Path

import pytest

import analytics_toolkit.cli as cli_module
from analytics_toolkit.cli import main
from agent_tools.docs_assistant import (
    _split_text,
    ask_docs,
    build_docs_index,
    chunk_markdown_file,
    discover_markdown_files,
    index_freshness_warnings,
    search_docs,
)


def test_discover_markdown_files_includes_readme_and_docs_only(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    (root / "README.md").write_text("# Project\n", encoding="utf-8")
    (root / "docs").mkdir()
    (root / "docs" / "guide.md").write_text("# Guide\n", encoding="utf-8")
    (root / "agent_docs").mkdir()
    (root / "agent_docs" / "development.md").write_text(
        "# Development\n", encoding="utf-8"
    )
    (root / "agent_tools").mkdir()
    (root / "agent_tools" / "README.md").write_text("# Agent Tools\n", encoding="utf-8")
    (root / "agent_tools" / "internal.md").write_text(
        "# Internal Tool Notes\n", encoding="utf-8"
    )
    (root / "analytics_toolkit").mkdir()
    (root / "analytics_toolkit" / "internal.md").write_text("# Internal\n", encoding="utf-8")

    files = [path.relative_to(root).as_posix() for path in discover_markdown_files(root)]

    assert files == [
        "README.md",
        "agent_docs/development.md",
        "agent_tools/README.md",
        "docs/guide.md",
    ]


def test_chunk_markdown_file_preserves_heading_and_function_metadata(tmp_path: Path) -> None:
    root = tmp_path / "project"
    function_dir = root / "docs" / "modules" / "ab_utils" / "functions"
    function_dir.mkdir(parents=True)
    page = function_dir / "compute-test-metrics.md"
    page.write_text(
        "\n".join(
            [
                "[All AB functions](index.md)",
                "",
                "# compute_test_metrics",
                "",
                "Compare experiment groups.",
                "",
                "```python",
                "compute_test_metrics(df, group='group_name')",
                "```",
                "",
                "## Inputs",
                "",
                "- `df`: Experiment dataframe.",
            ]
        ),
        encoding="utf-8",
    )

    chunks = chunk_markdown_file(page, root)

    assert any(chunk.heading_path == ("compute_test_metrics",) for chunk in chunks)
    assert any(chunk.heading_path == ("compute_test_metrics", "Inputs") for chunk in chunks)
    assert {chunk.function_name for chunk in chunks} == {"compute_test_metrics"}
    assert all(chunk.module == "ab_utils" for chunk in chunks)
    assert all(chunk.is_function_doc for chunk in chunks)
    assert {chunk.source_type for chunk in chunks} == {"public_docs"}


def test_build_index_and_search_retrieves_function_and_workflow_docs(tmp_path: Path) -> None:
    root = _write_sample_docs(tmp_path / "project")
    index_dir = tmp_path / "rag-index"

    result = build_docs_index(root=root, index_dir=index_dir)

    manifest = json.loads((index_dir / "manifest.json").read_text(encoding="utf-8"))
    assert result.file_count == 7
    assert result.chunk_count >= 4
    assert manifest["retrieval"] == "lexical"
    assert manifest["tool"] == "agent_tools/docs_assistant.py"
    assert {source["path"] for source in manifest["source_files"]} == {
        "README.md",
        "agent_docs/development.md",
        "agent_tools/README.md",
        "docs/CHANGELOG.md",
        "docs/modules/ab_utils/functions/compute-test-metrics.md",
        "docs/modules/excel/formatting-and-output.md",
        "docs/modules/sql/configuration.md",
    }

    metric_results = search_docs(
        "compute_test_metrics ratio metric inputs",
        index_dir=index_dir,
        top_k=1,
    )
    trino_results = search_docs(
        "How do I configure Trino?",
        index_dir=index_dir,
        top_k=1,
    )
    excel_results = search_docs(
        "Excel enforce_same_row_order extra row labels",
        index_dir=index_dir,
        top_k=1,
    )
    rag_results = search_docs(
        "docs_assistant RAG workflow",
        index_dir=index_dir,
        top_k=1,
    )
    ask_results = search_docs(
        "how to run no-llm ask",
        index_dir=index_dir,
        top_k=1,
    )
    precommit_results = search_docs(
        "development instructions pre commit checks",
        index_dir=index_dir,
        top_k=1,
    )
    broad_public_results = search_docs(
        "agent instructions compute_test_metrics ratio metric inputs",
        index_dir=index_dir,
        top_k=1,
    )

    assert metric_results[0].chunk.path == (
        "docs/modules/ab_utils/functions/compute-test-metrics.md"
    )
    assert metric_results[0].chunk.source_type == "public_docs"
    assert trino_results[0].chunk.path == "docs/modules/sql/configuration.md"
    assert trino_results[0].chunk.source_type == "public_docs"
    assert excel_results[0].chunk.path == "docs/modules/excel/formatting-and-output.md"
    assert rag_results[0].chunk.path == "agent_tools/README.md"
    assert rag_results[0].chunk.source_type == "agent_tools"
    assert ask_results[0].chunk.path == "agent_tools/README.md"
    assert precommit_results[0].chunk.path == "agent_docs/development.md"
    assert precommit_results[0].chunk.source_type == "agent_docs"
    assert broad_public_results[0].chunk.path == (
        "docs/modules/ab_utils/functions/compute-test-metrics.md"
    )
    assert broad_public_results[0].chunk.source_type == "public_docs"


def test_ask_docs_without_llm_returns_grounded_passages_and_citations(tmp_path: Path) -> None:
    root = _write_sample_docs(tmp_path / "project")
    index_dir = tmp_path / "rag-index"
    build_docs_index(root=root, index_dir=index_dir)

    answer = ask_docs("How do I configure Trino?", index_dir=index_dir)

    assert "Most relevant passages" in answer.answer
    assert "docs/modules/sql/configuration.md:L" in answer.answer
    assert answer.citations[0].startswith("docs/modules/sql/configuration.md:L")


def test_ask_docs_reports_insufficient_context_for_unknown_questions(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    (root / "README.md").write_text("# analytics_toolkit\nDate helpers.\n", encoding="utf-8")
    index_dir = tmp_path / "rag-index"
    build_docs_index(root=root, index_dir=index_dir)

    answer = ask_docs("private nonpublic vector shard retry internals", index_dir=index_dir)

    assert not answer.results
    assert "could not find enough relevant documentation context" in answer.answer


def test_agent_docs_cli_indexes_searches_and_answers_with_sources(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from agent_tools.docs_assistant import main as docs_main

    root = _write_sample_docs(tmp_path / "project")
    index_dir = tmp_path / "rag-index"

    assert docs_main(["index", "--root", str(root), "--index-dir", str(index_dir)]) == 0
    index_output = capsys.readouterr().out
    assert "Indexed" in index_output

    assert docs_main(
        [
            "search",
            "How do I configure Trino?",
            "--index-dir",
            str(index_dir),
            "--top-k",
            "1",
        ]
    ) == 0
    search_output = capsys.readouterr().out
    assert "docs/modules/sql/configuration.md:L" in search_output
    assert "source=public_docs" in search_output

    assert docs_main(
        [
            "ask",
            "--no-llm",
            "How do I configure Trino?",
            "--index-dir",
            str(index_dir),
        ]
    ) == 0
    ask_output = capsys.readouterr().out
    assert "Most relevant passages" in ask_output
    assert "Sources:" in ask_output
    assert "docs/modules/sql/configuration.md:L" in ask_output


def test_agent_docs_cli_warns_when_index_sources_are_stale(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from agent_tools.docs_assistant import main as docs_main

    root = _write_sample_docs(tmp_path / "project")
    index_dir = tmp_path / "rag-index"
    agent_tools_readme = root / "agent_tools" / "README.md"

    assert docs_main(["index", "--root", str(root), "--index-dir", str(index_dir)]) == 0
    capsys.readouterr()

    current_mtime_ns = agent_tools_readme.stat().st_mtime_ns
    agent_tools_readme.write_text(
        agent_tools_readme.read_text(encoding="utf-8")
        + "\nSearch commands warn when this file changes.\n",
        encoding="utf-8",
    )
    stale_mtime_ns = current_mtime_ns + 10_000_000_000
    os.utime(agent_tools_readme, ns=(stale_mtime_ns, stale_mtime_ns))

    assert docs_main(
        [
            "search",
            "docs_assistant RAG workflow",
            "--index-dir",
            str(index_dir),
            "--top-k",
            "1",
        ]
    ) == 0
    search_output = capsys.readouterr()
    assert "agent_tools/README.md:L" in search_output.out
    assert "Warning: Docs index is stale: agent_tools/README.md changed after indexing." in (
        search_output.err
    )

    assert docs_main(
        [
            "ask",
            "--no-llm",
            "how to run no-llm ask",
            "--index-dir",
            str(index_dir),
            "--top-k",
            "1",
        ]
    ) == 0
    ask_output = capsys.readouterr()
    assert "agent_tools/README.md:L" in ask_output.out
    assert "Warning: Docs index is stale: agent_tools/README.md changed after indexing." in (
        ask_output.err
    )


def test_index_freshness_warns_for_new_and_removed_source_files(tmp_path: Path) -> None:
    root = _write_sample_docs(tmp_path / "project")
    index_dir = tmp_path / "rag-index"
    build_docs_index(root=root, index_dir=index_dir)

    new_doc = root / "agent_docs" / "review.md"
    new_doc.write_text("# Review\n\nUse focused review loops.\n", encoding="utf-8")
    removed_doc = root / "docs" / "modules" / "excel" / "formatting-and-output.md"
    removed_doc.unlink()

    warnings = index_freshness_warnings(index_dir)

    assert "Docs index is stale: new source file is not indexed: agent_docs/review.md" in warnings
    assert "Indexed source file is missing: docs/modules/excel/formatting-and-output.md" in warnings


def test_split_text_rejects_overlap_that_cannot_advance() -> None:
    with pytest.raises(ValueError, match="overlap_chars must be smaller than max_chars"):
        _split_text("abcdef", max_chars=3, overlap_chars=3)


def test_public_cli_no_longer_exposes_docs_commands(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["docs", "index"])

    assert exc_info.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_cli_prefers_local_package_path_for_console_namespace_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shadow_package = tmp_path / "site-packages" / "analytics_toolkit"
    shadow_package.mkdir(parents=True)
    package = types.SimpleNamespace(__path__=[str(shadow_package)])
    monkeypatch.setitem(sys.modules, "analytics_toolkit", package)

    cli_module._prefer_local_package_path()

    assert Path(package.__path__[0]).resolve() == Path(cli_module.__file__).parent
    assert package.__path__[1:] == [str(shadow_package)]


def _write_sample_docs(root: Path) -> Path:
    root.mkdir()
    (root / "README.md").write_text(
        "\n".join(
            [
                "# analytics_toolkit",
                "",
                "Public docs for analytics helpers.",
            ]
        ),
        encoding="utf-8",
    )

    agent_docs = root / "agent_docs"
    agent_docs.mkdir()
    (agent_docs / "development.md").write_text(
        "\n".join(
            [
                "# Development Agent Instructions",
                "",
                "Run release_routines/pre_commit_checks.sh before every commit.",
                "Use pre_commit checks for repository validation.",
            ]
        ),
        encoding="utf-8",
    )

    agent_tools = root / "agent_tools"
    agent_tools.mkdir()
    (agent_tools / "README.md").write_text(
        "\n".join(
            [
                "# Agent Tools",
                "",
                "Use docs_assistant.py for local RAG documentation retrieval.",
                "",
                "Run `python agent_tools/docs_assistant.py index` before search.",
                "Run `python agent_tools/docs_assistant.py search \"topic\" --top-k 5`.",
                "Run `python agent_tools/docs_assistant.py ask --no-llm \"question\"`.",
            ]
        ),
        encoding="utf-8",
    )

    docs_dir = root / "docs"
    docs_dir.mkdir()
    (docs_dir / "CHANGELOG.md").write_text(
        "\n".join(
            [
                "# Changelog",
                "",
                "## 1.0.0",
                "",
                "- Added trino_catalog to table listings for Trino aliases.",
                "- Added source metadata to the docs assistant RAG workflow.",
            ]
        ),
        encoding="utf-8",
    )

    ab_functions = root / "docs" / "modules" / "ab_utils" / "functions"
    ab_functions.mkdir(parents=True)
    (ab_functions / "compute-test-metrics.md").write_text(
        "\n".join(
            [
                "[All AB functions](index.md)",
                "",
                "# compute_test_metrics",
                "",
                "Compare experiment groups across mean and ratio metrics.",
                "",
                "```python",
                "compute_test_metrics(df, group='group_name', ratio_metrics=None)",
                "```",
                "",
                "## Inputs",
                "",
                "- `ratio_metrics`: Optional ratio metric specifications.",
            ]
        ),
        encoding="utf-8",
    )

    sql_dir = root / "docs" / "modules" / "sql"
    sql_dir.mkdir(parents=True)
    (sql_dir / "configuration.md").write_text(
        "\n".join(
            [
                "[SQL module index](index.md)",
                "",
                "# SQL Configuration",
                "",
                "Connection aliases live in a `.connections` JSON file.",
                "",
                "## Trino Connections",
                "",
                "A Trino alias uses `type` set to `trino`, plus host, port, user, catalog, and schema.",
            ]
        ),
        encoding="utf-8",
    )

    excel_dir = root / "docs" / "modules" / "excel"
    excel_dir.mkdir(parents=True)
    (excel_dir / "formatting-and-output.md").write_text(
        "\n".join(
            [
                "[Excel helpers index](index.md)",
                "",
                "# Formatting And Output",
                "",
                "`enforce_same_row_order=True` aligns later dataframe tables to the first dataframe.",
                "It rejects extra row labels.",
            ]
        ),
        encoding="utf-8",
    )
    return root
