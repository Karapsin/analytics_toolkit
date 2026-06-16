# Agent Tools

This directory contains repository-local tools for coding agents working in this
checkout. These tools are not part of the public `analytics-toolkit` package API
and are not installed through package extras.

## MCP Interface

Use the repository-local MCP server as the normal agent interface for startup,
documentation retrieval, workflow status, version bumps, checks, git workflow,
and release workflow entrypoints. For terminal validation and planning, use the
wrapper:

```bash
agent_tools/mcp_tool.sh prepare-start --task "implementation" --module agent_tools
agent_tools/mcp_tool.sh docs "specific topic" --mode search --top-k 5
agent_tools/mcp_tool.sh workflow-status --task "implementation" --module agent_tools
agent_tools/mcp_tool.sh version-bump "Updated agent workflow" --dry-run
agent_tools/mcp_tool.sh run-checks --area agent_tools --level focused --dry-run
agent_tools/mcp_tool.sh release-workflow --action status
```

Use `git-workflow commit` only when the current batch is ready to commit, and
use `release-workflow --action publish` only when release readiness is clean.

`mcp_server.py` exposes the consolidated tool surface:
`prepare_start`, `docs`, `workflow_status`, `version_bump`, `run_checks`,
`git_workflow`, and `release_workflow`.

## Docs Assistant

`docs_assistant.py` is the implementation behind `prepare_start` RAG indexing
and `docs(...)` retrieval. It indexes `README.md` plus Markdown files under
`docs/`, `agent_docs/`, and `agent_tools/README.md` into `.rag_index/`, then
uses heading-aware chunks and lexical BM25-like scoring to return grounded
passages with citations.

RAG is intentionally agent-only in this repository. The docs assistant is
stdlib-only and must stay outside public package CLI commands and package
extras. It does not use vector stores, hosted LLM SDKs, Ollama, or embedding
models.
