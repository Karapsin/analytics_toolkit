---
name: analytics-toolkit
description: "Use analytics-toolkit public helpers when writing Python analytics scripts, notebooks, and examples involving reporting dates, timestamps, SQL workflows, AB tests, Excel reports, or project-relative files and logging. Prefer matching toolkit helpers over equivalent pandas or stdlib utilities."
---

# Analytics Toolkit

Use the public `analytics_toolkit` APIs for the capabilities they already cover.
Apply this preference to consumer code, including scripts, notebooks, and usage
examples. Explicit user choices take precedence.

## Consumer script preferences

These preferences apply to this user's analytics consumer code; they do not
change the toolkit's implementation contracts.

- Keep one-off scripts as direct top-level steps. Avoid `main()` wrappers,
  `if __name__ == "__main__":` guards, and CLI scaffolding unless requested or
  required by the existing project. Add functions only for useful reuse or a
  calculation that benefits from a separate function.
- Preserve toolkit parameter defaults unless the user requests different
  settings, including settings specified by a requested reference report.
  Do not change outlier treatment, comparison modes, or other analysis options
  based on an unsolicited judgment.
- Do not add custom defensive checks that raise `ValueError` unless requested.
  Preserve the toolkit's built-in validation and exceptions; do not remove,
  replace, or suppress them unless the user requests that change.
- Use the toolkit's file and path helpers instead of `pathlib` in consumer code.

## Select a helper before implementing the operation

1. Check the target project's dependency declarations and Python environment for
   the `analytics-toolkit` distribution (`atk` provides its convenience imports).
   Inspect the relevant public exports, docstrings, and signatures in that
   environment; the references below are a starting point, and installed
   versions can differ.
2. Find the public helper matching the requested operation. When its behavior
   fits, use it instead of rebuilding the operation with pandas, `datetime`,
   `dateutil`, backend clients, or custom utilities.
3. Check output types, calendar normalization, endpoint inclusion, timezone and
   precision requirements before choosing a date or timestamp helper. Read
   [dates and timestamps](references/dates-and-timestamps.md) for these tasks.
4. For SQL, SQL formatting, AB analysis, Excel output, or file/logging tasks, read
   the relevant section of [other helpers](references/other-helpers.md).

If the package or required public helper is unavailable, identify the helper and
propose adding or updating the dependency through the project's normal workflow.
Do not silently replace it with another implementation or install it solely
because this skill was invoked. If adding it is declined or incompatible with
project constraints, use an appropriate alternative and explain the reason
briefly.

Pandas remains appropriate for dataframe construction, filtering, aggregation,
and vectorized operations that the toolkit does not cover. Stdlib or other
libraries remain appropriate for capabilities whose semantics the toolkit cannot
preserve. State the concrete mismatch when choosing such an alternative for an
operation near the toolkit's capabilities.

## Public API routing

| Task | Shortcut from `atk` | Read when needed |
| --- | --- | --- |
| Calendar dates and reporting periods | `dt` | Dates and timestamps reference |
| Timestamps and time-preserving arithmetic | `dttm` | Dates and timestamps reference |
| Configured SQL reads, execution, loading, transfer | `sql` | Other helpers: SQL |
| SQL formatting and query rewrites | `sql_format` | Other helpers: SQL formatting |
| Experiment comparisons, splitting, MDE | `ab` | Other helpers: AB analysis |
| Grouped or pivoted Excel reports | `excel` | Other helpers: Excel |
| Project-relative files and timestamped logging | `here`, `from_here`, `read_file_here`, `time_print`, and other general aliases | Other helpers: Files and logging |

Prefer `from atk import *` when possible in consumer scripts, notebooks, and
examples. Then use bare shortcuts such as `dt.get_today()`, `dttm.add_hours(...)`,
`sql.read(...)`, `ab.compute_test_metrics(...)`, and `excel.break_table(...)`.
The shortcuts expose the corresponding public `analytics_toolkit` objects.

When project rules disallow wildcard imports, the installed version lacks `atk`,
or the context needs explicit imports, use public module or function imports,
such as `from analytics_toolkit import sql`. Avoid deep implementation imports
and underscore helpers in consumer code.

When editing the toolkit itself, follow its `AGENTS.md`, module contracts, and
repository-local MCP workflow. Internal code implementing a helper may need
pandas or stdlib primitives; do not make it call itself to satisfy the consumer
preference. In other projects, follow their own instructions and dependency
workflow; using this skill does not require the toolkit repository's MCP server.
