[Documentation overview](README.md)

# RAG Docs Assistant

The `analytics-toolkit docs` CLI commands build and query a local RAG index for
the public project documentation. The index includes `README.md` and Markdown
files under `docs/`, then returns answers with citations back to the source
documentation files.

RAG support depends on optional packages. Install the local RAG dependencies
before using dense retrieval or Ollama answer generation:

```bash
pip install "analytics-toolkit[rag]"
```

Hosted providers are opt-in. Install the provider extra you need, or install
all RAG providers. The `rag-all` extra is required when you want every local and
hosted RAG path available in one environment:

```bash
pip install "analytics-toolkit[rag-openai]"
pip install "analytics-toolkit[rag-anthropic]"
pip install "analytics-toolkit[rag-gemini]"
pip install "analytics-toolkit[rag-all]"
```

Build the index from the repository root:

```bash
analytics-toolkit docs index
```

The default index directory is `.rag_index/`, which is local-only and ignored by
git. Dense retrieval uses a local sentence-transformers model and Chroma when
those optional dependencies are installed. If they are missing, the command
still creates a lexical index and reports that dense retrieval was skipped.

To build a dense local index with a specific local model:

```bash
analytics-toolkit docs index \
  --embedding-provider sentence-transformers \
  --embedding-model sentence-transformers/all-MiniLM-L6-v2
```

To build a dense hosted embedding index, select a hosted embedding provider.
The vector database still stays local in `.rag_index/`, but documentation chunks
are sent to the selected embedding API during indexing:

```bash
OPENAI_API_KEY=... analytics-toolkit docs index \
  --embedding-provider openai \
  --embedding-model text-embedding-3-small

GEMINI_API_KEY=... analytics-toolkit docs index \
  --embedding-provider gemini \
  --embedding-model gemini-embedding-001
```

OpenAI-compatible embedding endpoints use the OpenAI SDK plus a custom base URL:

```bash
OPENAI_API_KEY=... analytics-toolkit docs index \
  --embedding-provider openai-compatible \
  --embedding-model text-embedding-3-small \
  --base-url https://example.local/v1
```

Search without answer generation:

```bash
analytics-toolkit docs search "compute_test_metrics ratio metrics"
```

Ask a question with local Ollama generation:

```bash
analytics-toolkit docs ask "How do I configure Trino?"
```

Ask with a hosted generation provider. Retrieved chunks and the question are
sent to the selected provider; the local index remains local:

```bash
OPENAI_API_KEY=... analytics-toolkit docs ask \
  --llm-provider openai \
  --model gpt-4.1-mini \
  "How do I configure Trino?"

ANTHROPIC_API_KEY=... analytics-toolkit docs ask \
  --llm-provider anthropic \
  --model claude-sonnet-4-5 \
  "What does compute_test_metrics output?"

GEMINI_API_KEY=... analytics-toolkit docs ask \
  --llm-provider gemini \
  --model gemini-2.5-flash \
  "How do Excel row ordering rules work?"
```

OpenAI-compatible generation endpoints use `--llm-provider openai-compatible`
and `--base-url`.

To return grounded retrieved passages without calling Ollama:

```bash
analytics-toolkit docs ask --no-llm "How do I configure Trino?"
```

The assistant is intended for documented public behavior. If the local docs do
not contain enough context, it should say that instead of inventing private
implementation details.

[Documentation overview](README.md)
