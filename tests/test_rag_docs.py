from __future__ import annotations

from pathlib import Path

from analytics_toolkit.cli import main
from analytics_toolkit.rag_docs import (
    ask_docs,
    build_docs_index,
    chunk_markdown_file,
    discover_markdown_files,
    search_docs,
)


def test_discover_markdown_files_includes_readme_and_docs_only(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    (root / "README.md").write_text("# Project\n", encoding="utf-8")
    (root / "docs").mkdir()
    (root / "docs" / "guide.md").write_text("# Guide\n", encoding="utf-8")
    (root / "analytics_toolkit").mkdir()
    (root / "analytics_toolkit" / "internal.md").write_text("# Internal\n", encoding="utf-8")

    files = [path.relative_to(root).as_posix() for path in discover_markdown_files(root)]

    assert files == ["README.md", "docs/guide.md"]


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


def test_build_index_and_search_retrieves_function_and_workflow_docs(tmp_path: Path) -> None:
    root = _write_sample_docs(tmp_path / "project")
    index_dir = tmp_path / "rag-index"

    result = build_docs_index(root=root, index_dir=index_dir, dense=False)

    assert result.file_count == 4
    assert result.chunk_count >= 4
    assert not result.dense_enabled

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

    assert metric_results[0].chunk.path == (
        "docs/modules/ab_utils/functions/compute-test-metrics.md"
    )
    assert trino_results[0].chunk.path == "docs/modules/sql/configuration.md"
    assert excel_results[0].chunk.path == "docs/modules/excel/formatting-and-output.md"


def test_ask_docs_without_llm_returns_grounded_passages_and_citations(tmp_path: Path) -> None:
    root = _write_sample_docs(tmp_path / "project")
    index_dir = tmp_path / "rag-index"
    build_docs_index(root=root, index_dir=index_dir, dense=False)

    answer = ask_docs(
        "How do I configure Trino?",
        index_dir=index_dir,
        use_llm=False,
    )

    assert not answer.used_llm
    assert "Most relevant passages" in answer.answer
    assert "docs/modules/sql/configuration.md:L" in answer.answer
    assert answer.citations[0].startswith("docs/modules/sql/configuration.md:L")


def test_ask_docs_reports_insufficient_context_for_unknown_questions(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    (root / "README.md").write_text("# analytics_toolkit\nDate helpers.\n", encoding="utf-8")
    index_dir = tmp_path / "rag-index"
    build_docs_index(root=root, index_dir=index_dir, dense=False)

    answer = ask_docs(
        "private nonpublic vector shard retry internals",
        index_dir=index_dir,
        use_llm=False,
    )

    assert not answer.results
    assert "could not find enough relevant documentation context" in answer.answer


def test_docs_cli_indexes_and_answers_with_sources(
    tmp_path: Path,
    capsys,
) -> None:
    root = _write_sample_docs(tmp_path / "project")
    index_dir = tmp_path / "rag-index"

    assert main(
        [
            "docs",
            "index",
            "--root",
            str(root),
            "--index-dir",
            str(index_dir),
            "--no-dense",
        ]
    ) == 0
    index_output = capsys.readouterr().out
    assert "Indexed" in index_output
    assert "Dense retrieval disabled" in index_output

    assert main(
        [
            "docs",
            "ask",
            "How do I configure Trino?",
            "--index-dir",
            str(index_dir),
            "--no-llm",
        ]
    ) == 0
    ask_output = capsys.readouterr().out
    assert "Most relevant passages" in ask_output
    assert "Sources:" in ask_output
    assert "docs/modules/sql/configuration.md:L" in ask_output


def _write_sample_docs(root: Path) -> Path:
    root.mkdir()
    (root / "README.md").write_text(
        "\n".join(
            [
                "# analytics_toolkit",
                "",
                "Public docs for analytics helpers.",
                "",
                "## Local Docs Assistant",
                "",
                'Run `analytics-toolkit docs ask "How do I configure Trino?"`.',
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
