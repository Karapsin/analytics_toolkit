from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

from analytics_toolkit.cli import main
from analytics_toolkit.rag_docs import (
    ask_docs,
    build_docs_index,
    chunk_markdown_file,
    discover_markdown_files,
    search_docs,
)
from analytics_toolkit.rag_docs.providers import (
    RagProviderError,
    build_embedding_provider,
    build_generation_provider,
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


def test_openai_embedding_provider_builds_dense_local_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_chromadb(monkeypatch)
    install_fake_openai(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "test-token")
    root = _write_sample_docs(tmp_path / "project")
    index_dir = tmp_path / "rag-index"

    result = build_docs_index(
        root=root,
        index_dir=index_dir,
        embedding_provider="openai",
        embedding_model="embed-test",
    )

    manifest = json.loads((index_dir / "manifest.json").read_text(encoding="utf-8"))
    assert result.dense_enabled
    assert manifest["embedding_provider"] == "openai"
    assert manifest["embedding_model"] == "embed-test"

    results = search_docs(
        "How do I configure Trino?",
        index_dir=index_dir,
        embedding_provider="openai",
        embedding_model="embed-test",
    )
    assert results

    with pytest.raises(ValueError, match="Dense index was built with embedding model"):
        search_docs(
            "How do I configure Trino?",
            index_dir=index_dir,
            embedding_provider="openai",
            embedding_model="other-model",
        )


def test_openai_compatible_embedding_requires_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_openai(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "test-token")
    provider = build_embedding_provider(
        "openai-compatible",
        model="embed-test",
    )

    with pytest.raises(RagProviderError, match="--base-url"):
        provider.embed_documents(["hello"])


@pytest.mark.parametrize(
    ("provider", "env_name", "model", "installer", "expected"),
    [
        ("openai", "OPENAI_API_KEY", "gpt-test", "openai", "openai answer"),
        ("anthropic", "ANTHROPIC_API_KEY", "claude-test", "anthropic", "anthropic answer"),
        ("gemini", "GEMINI_API_KEY", "gemini-test", "gemini", "gemini answer"),
    ],
)
def test_generation_providers_use_hosted_sdks(
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    env_name: str,
    model: str,
    installer: str,
    expected: str,
) -> None:
    if installer == "openai":
        install_fake_openai(monkeypatch)
    elif installer == "anthropic":
        install_fake_anthropic(monkeypatch)
    elif installer == "gemini":
        install_fake_gemini(monkeypatch)
    monkeypatch.setenv(env_name, "test-token")

    result = build_generation_provider(provider, model=model).answer(
        "question",
        "[1] context",
    )

    assert result == expected


def test_cli_ask_uses_hosted_generation_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    install_fake_openai(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "test-token")
    root = _write_sample_docs(tmp_path / "project")
    index_dir = tmp_path / "rag-index"
    build_docs_index(root=root, index_dir=index_dir, dense=False)

    assert main(
        [
            "docs",
            "ask",
            "How do I configure Trino?",
            "--index-dir",
            str(index_dir),
            "--llm-provider",
            "openai",
            "--model",
            "gpt-test",
        ]
    ) == 0

    output = capsys.readouterr().out
    assert "openai answer" in output
    assert "Sources:" in output


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


def install_fake_chromadb(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    collections: dict[str, FakeCollection] = {}

    class FakeClient:
        def __init__(self, path: str) -> None:
            self.path = path

        def delete_collection(self, name: str) -> None:
            collections.pop(name, None)

        def create_collection(
            self,
            name: str,
            metadata: dict[str, object] | None = None,
        ) -> "FakeCollection":
            del metadata
            collection = FakeCollection()
            collections[name] = collection
            return collection

        def get_collection(self, name: str) -> "FakeCollection":
            return collections[name]

    fake_module = types.ModuleType("chromadb")
    fake_module.PersistentClient = FakeClient
    monkeypatch.setitem(sys.modules, "chromadb", fake_module)
    return collections


class FakeCollection:
    def __init__(self) -> None:
        self.ids: list[str] = []
        self.documents: list[str] = []
        self.embeddings: list[list[float]] = []
        self.metadatas: list[dict[str, object]] = []

    def add(
        self,
        ids: list[str],
        documents: list[str],
        embeddings: list[list[float]],
        metadatas: list[dict[str, object]],
    ) -> None:
        self.ids = ids
        self.documents = documents
        self.embeddings = embeddings
        self.metadatas = metadatas

    def query(
        self,
        query_embeddings: list[list[float]],
        n_results: int,
        include: list[str],
    ) -> dict[str, list[list[object]]]:
        del query_embeddings, include
        ids = self.ids[:n_results]
        distances = [float(index) / max(1, len(ids)) for index, _ in enumerate(ids)]
        return {"ids": [ids], "distances": [distances]}


def install_fake_openai(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeEmbeddings:
        def create(self, model: str, input: list[str]) -> object:
            del model
            data = [
                types.SimpleNamespace(embedding=[float(len(text)), 1.0])
                for text in input
            ]
            return types.SimpleNamespace(data=data)

    class FakeResponses:
        def create(self, model: str, input: list[dict[str, str]]) -> object:
            del model, input
            return types.SimpleNamespace(output_text="openai answer")

    class FakeCompletions:
        def create(self, model: str, messages: list[dict[str, str]]) -> object:
            del model, messages
            message = types.SimpleNamespace(content="openai answer")
            choice = types.SimpleNamespace(message=message)
            return types.SimpleNamespace(choices=[choice])

    class FakeOpenAI:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs
            self.embeddings = FakeEmbeddings()
            self.responses = FakeResponses()
            self.chat = types.SimpleNamespace(
                completions=FakeCompletions(),
            )

    fake_module = types.ModuleType("openai")
    fake_module.OpenAI = FakeOpenAI
    monkeypatch.setitem(sys.modules, "openai", fake_module)


def install_fake_anthropic(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeMessages:
        def create(self, **kwargs: object) -> object:
            del kwargs
            return types.SimpleNamespace(
                content=[types.SimpleNamespace(text="anthropic answer")]
            )

    class FakeAnthropic:
        def __init__(self, api_key: str) -> None:
            self.api_key = api_key
            self.messages = FakeMessages()

    fake_module = types.ModuleType("anthropic")
    fake_module.Anthropic = FakeAnthropic
    monkeypatch.setitem(sys.modules, "anthropic", fake_module)


def install_fake_gemini(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeModels:
        def embed_content(self, model: str, contents: list[str]) -> object:
            del model
            embeddings = [
                types.SimpleNamespace(values=[float(len(text)), 1.0])
                for text in contents
            ]
            return types.SimpleNamespace(embeddings=embeddings)

        def generate_content(self, model: str, contents: str) -> object:
            del model, contents
            return types.SimpleNamespace(text="gemini answer")

    class FakeClient:
        def __init__(self, api_key: str) -> None:
            self.api_key = api_key
            self.models = FakeModels()

    google_module = types.ModuleType("google")
    genai_module = types.ModuleType("google.genai")
    genai_module.Client = FakeClient
    google_module.genai = genai_module
    monkeypatch.setitem(sys.modules, "google", google_module)
    monkeypatch.setitem(sys.modules, "google.genai", genai_module)
