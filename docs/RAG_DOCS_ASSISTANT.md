[Documentation overview](README.md)

# RAG Docs Assistant

The `analytics-toolkit docs` CLI commands build and query a local RAG index for
the public project documentation. The index includes `README.md` and Markdown
files under `docs/`, then returns answers with citations back to the source
documentation files.

Install the optional local RAG dependencies before using dense retrieval or
Ollama answer generation:

```bash
pip install "analytics-toolkit[rag]"
```

Build the index from the repository root:

```bash
analytics-toolkit docs index
```

The default index directory is `.rag_index/`, which is local-only and ignored by
git. Dense retrieval uses a local sentence-transformers model and Chroma when
those optional dependencies are installed. If they are missing, the command
still creates a lexical index and reports that dense retrieval was skipped.

Search without answer generation:

```bash
analytics-toolkit docs search "compute_test_metrics ratio metrics"
```

Ask a question with local Ollama generation:

```bash
analytics-toolkit docs ask "How do I configure Trino?"
```

To return grounded retrieved passages without calling Ollama:

```bash
analytics-toolkit docs ask --no-llm "How do I configure Trino?"
```

The assistant is intended for documented public behavior. If the local docs do
not contain enough context, it should say that instead of inventing private
implementation details.

[Documentation overview](README.md)
