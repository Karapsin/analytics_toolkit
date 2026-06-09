# Agent Tools

This directory contains repository-local tools for coding agents working in this
checkout. These tools are not part of the public `analytics-toolkit` package API
and are not installed through package extras.

## Docs Assistant

Use `docs_assistant.py` for local documentation retrieval:

```bash
python agent_tools/docs_assistant.py index
python agent_tools/docs_assistant.py search "<topic or function name>" --top-k 5
python agent_tools/docs_assistant.py ask --no-llm "<specific question>"
```

RAG is intentionally agent-only in this repository. The docs assistant indexes
`README.md` plus Markdown files under `docs/` into `.rag_index/`, then uses
heading-aware chunks and lexical BM25-like scoring to return grounded passages
with citations. It is stdlib-only and must stay outside public package CLI
commands and package extras. It does not use vector stores, hosted LLM SDKs,
Ollama, or embedding models.
