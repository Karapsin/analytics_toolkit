# AGENTS.md

## Scope

These instructions apply to the whole repository.

## Mandatory Startup Sync

Before any repository-related action, run:

```bash
git pull origin main
```

This pull must happen before RAG indexing/search, file inspection, tests, or
edits. If it fails because of local changes, merge conflicts, authentication,
network issues, or divergent history, stop and report the blocker instead of
continuing with stale code.

## Project Overview

`analytics_toolkit` is a Python 3.11+ utility package with five public areas:

- `analytics_toolkit.ab_utils`: AB-test metric comparison helpers.
- `analytics_toolkit.sql`: SQL read/execute/load/transfer helpers for Greenplum, Trino, and ClickHouse.
- `analytics_toolkit.excel`: long-format dataframe to Excel report helpers.
- `analytics_toolkit.dates`: date and period helpers.
- `analytics_toolkit.general`: shared logging and file path helpers.

Keep public APIs stable unless the user explicitly asks for a breaking change.
Many tests import underscore helpers through package re-export modules, so treat
exported internals as compatibility surface too.

## Required Context Routing

Root `AGENTS.md` is the auto-discovered instruction file. The files under
`agent_docs/` are not auto-loaded unless this file routes you to them.

After the mandatory startup sync and RAG pass, read the relevant files before
normal repository inspection, tests, or edits:

- Any implementation, testing, build, or commit work: `agent_docs/development.md`.
- Public documentation work under `docs/` or README documentation sections: `agent_docs/documentation.md`.
- PyPI publishing, package releases, or release workflow changes: `agent_docs/release.md`.
- SQL module work: `agent_docs/sql.md`.
- AB utilities work: `agent_docs/ab_utils.md`.
- Excel helper work: `agent_docs/excel.md`.
- Date helper work: `agent_docs/dates.md`.
- General helper work: `agent_docs/general.md`.
- Instruction maintenance for this file or `agent_docs/`: read this file and the specific instruction files being edited.

If multiple categories apply, read all relevant files before editing. Keep
retrieved and opened context focused on the task.

For SQL work, user-facing imports should use `from analytics_toolkit import sql`
or `import analytics_toolkit.sql as sql`. Do not restore removed root implementation paths.

## Agent-Only RAG Context Workflow

For any repository-related work, use the local docs RAG workflow before normal
repository search or file inspection. This includes implementation, reviews,
documentation edits, usage examples, API explanations, behavior investigations,
and answers about project conventions. Skip RAG only for clearly non-repository
requests, such as simple shell/time/status commands unrelated to project
behavior.

RAG is intentionally an agent-only repository workflow, not a public
`analytics-toolkit` package feature. Keep docs retrieval tooling under
`agent_tools/`, keep it runnable from a checkout, and do not add public CLI
commands, package extras, vector-store dependencies, hosted LLM SDKs, Ollama,
or embedding-model dependencies for it.

```bash
python agent_tools/docs_assistant.py index
python agent_tools/docs_assistant.py search "<topic or function name>" --top-k 5
```

Use `python agent_tools/docs_assistant.py ask --no-llm "<specific question>"`
when a grounded summary is more useful than raw search snippets. Keep retrieved
context focused; rebuilding `.rag_index/` is local work and does not itself
consume LLM context tokens, but reading retrieved output does.

Treat normal repository search, file inspection, and tests as secondary context
after the RAG pass, not as substitutes for it. If RAG is unavailable, blocked,
or returns no useful context after rebuilding, explicitly report that fallback
was needed, then use normal repository search and file inspection. When fallback
was needed because docs were missing or unclear, finish by proposing the
specific documentation update that would make future RAG retrieval unambiguous.

## Global Rules

- Prefer small, local changes that follow existing module patterns.
- Do not alter packaging metadata or rewrite README/manual docs unless the task requires it.
- After every non-documentation repository change, bump the package version in `pyproject.toml` and update `docs/CHANGELOG.md` in the same change. Documentation-only changes must not bump the package version unless they are preparing a release artifact that needs a new version. Versions use four parts: `a.b.c.d`, and each component has a maximum value of `19`. For a normal repository change, increment `d`; for example, `1.3.6.6` -> `1.3.6.7`. If `d` is already `19`, increment `c` and reset `d` to `0`; for example, `1.3.6.19` -> `1.3.7.0`. Apply the same carry rule to higher components: `1.3.19.19` -> `1.4.0.0`, `1.19.19.19` -> `2.0.0.0`. Do not let any component exceed `19`.
- When changing dependency declarations in `pyproject.toml`, update the CRAN-style `Depends`, `Imports`, and `Suggests` dependency entries in `README.md`.
- When changing public behavior, update the relevant module README and focused tests.
- Do not run tests against real databases. Unit tests should use fake connections, monkeypatching, and the autouse env fixture in `tests/conftest.py`.
- Keep `.connections` out of the repo. Tests should create a temporary `.connections` and chdir into that temp project.
- Use existing structured parsers for SQL/table names (`sqlparse`, `sqlglot`) instead of ad hoc parsing where those modules already do the job.
- At the end of every non-documentation change, run `release_routines/pre_commit_checks.sh` before committing, even if focused tests were run earlier. For documentation-only changes, full checks are not required; run focused tests only when the documentation change affects tested paths or generated artifacts. Treat test failures and pytest warnings as issues to fix before finishing; the final test run should pass with no warning summary.
- Once a coherent batch of changes is done, run `git add . && git commit -m '...'`, replacing `...` with a short description of the changes.
