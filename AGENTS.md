# AGENTS.md

## Scope

These instructions apply to the whole repository.

## Project Overview

`analytics_toolkit` is a Python 3.11+ utility package with five public areas:

- `analytics_toolkit.ab_utils`: AB-test metric comparison helpers.
- `analytics_toolkit.sql`: SQL read/execute/load/transfer helpers for Greenplum, Trino, and ClickHouse.
- `analytics_toolkit.excel`: long-format dataframe to Excel report helpers.
- `analytics_toolkit.dates`: date and period helpers.
- `analytics_toolkit.general`: shared logging and file path helpers.

Keep public APIs stable unless the user explicitly asks for a breaking change. Many tests import underscore helpers through package re-export modules, so treat exported internals as compatibility surface too.

## Development Commands

Use release routines for repository-wide checks and releases. Run pre-commit checks with:

```bash
release_routines/pre_commit_checks.sh
```

The pre-commit script uses a temporary bytecode cache, runs compileall and pytest, and then runs the full tox matrix for Python 3.8 through 3.14 plus the Python 3.8 minimum-dependency environment.

Use a temporary bytecode cache when running focused Python commands from this sandboxed workspace:

```bash
PYTHONPYCACHEPREFIX=/tmp/utils_dev_pycache python -m compileall analytics_toolkit tests
PYTHONPYCACHEPREFIX=/tmp/utils_dev_pycache pytest -q
```

Focused test files:

```bash
PYTHONPYCACHEPREFIX=/tmp/utils_dev_pycache pytest -q tests/test_ab_utils_metrics.py
PYTHONPYCACHEPREFIX=/tmp/utils_dev_pycache pytest -q tests/test_excel_long_format.py
PYTHONPYCACHEPREFIX=/tmp/utils_dev_pycache pytest -q tests/test_general_read_file.py
PYTHONPYCACHEPREFIX=/tmp/utils_dev_pycache pytest -q tests/test_sql_connection_config.py tests/test_sql_retries.py tests/test_sql_load_table.py
```

Run `release_routines/pre_commit_checks.sh` before every commit. Do not commit unless all matrix environments pass; if an interpreter or dependency is missing, install it or explicitly report the blocker instead of skipping that environment.

Do not run tests against real databases. Unit tests should use fake connections, monkeypatching, and the autouse env fixture in `tests/conftest.py`.

## RAG Context Workflow

Before implementation work that depends on documented public behavior, refresh the local docs RAG index and query it for targeted context:

```bash
analytics-toolkit docs index
analytics-toolkit docs search "<topic or function name>" --top-k 5
```

Use `analytics-toolkit docs ask --no-llm "<specific question>"` when a grounded summary is more useful than raw search snippets. Keep retrieved context focused; rebuilding `.rag_index/` is local work and does not itself consume LLM context tokens, but reading retrieved output does.

If RAG dependencies are missing and dependency installation is allowed, install `analytics-toolkit[rag-all]`. If RAG is unavailable, blocked, or returns no useful context after rebuilding, fall back to normal repository search and file inspection. When fallback was needed because docs were missing or unclear, finish by proposing the specific documentation update that would make future RAG retrieval unambiguous.

## General Rules

- Prefer small, local changes that follow existing module patterns.
- Do not alter packaging metadata or rewrite README/manual docs unless the task requires it.
- After every non-documentation repository change, bump the package version in `pyproject.toml` and update `docs/CHANGELOG.md` in the same change. Documentation-only changes must not bump the package version unless they are preparing a release artifact that needs a new version. Versions use four parts: `a.b.c.d`, and each component has a maximum value of `19`. For a normal repository change, increment `d`; for example, `1.3.6.6` -> `1.3.6.7`. If `d` is already `19`, increment `c` and reset `d` to `0`; for example, `1.3.6.19` -> `1.3.7.0`. Apply the same carry rule to higher components: `1.3.19.19` -> `1.4.0.0`, `1.19.19.19` -> `2.0.0.0`. Do not let any component exceed `19`.
- When changing dependency declarations in `pyproject.toml`, update the CRAN-style `Depends`, `Imports`, and `Suggests` dependency entries in `README.md`.
- When changing public behavior, update the relevant module README and focused tests.
- When adding, removing, or renaming files under `docs/modules/<module>/`, update `docs/modules/README.md`, that module folder's `index.md`, and any affected function index navigation in the same change.
- `docs/README.md` is the top-level documentation overview. Keep it linked to quick start, the Airflow SQL manual, module documentation, and the changelog.
- All documentation must be reachable through hyperlinks starting from the root `README.md` to `docs/README.md` and then through the relevant overview, module, or function index pages. When adding, removing, or renaming a docs file, update the parent index or overview so the file is discoverable.
- Every documentation Markdown file should include a hyperlink back to the nearest index or overview: top-level `docs/*.md` files link to `docs/README.md`, `docs/modules/README.md` links to `docs/README.md`, module indexes link to `docs/modules/README.md`, top-level module workflow docs link to that module's `index.md`, and function docs link to that module's `functions/index.md`.
- `docs/modules/README.md` should link every module index and should start and end with a link back to `docs/README.md`.
- Every top-level module index at `docs/modules/<module>/index.md` should start and end with a link back to `docs/modules/README.md`.
- Every top-level module index should put the function reference first, before workflow guides, using `## All <Module> Functions`, then `## Workflow Guides`.
- Top-level module docs under `docs/modules/<module>/` should be concept/workflow guides. Keep exact function signatures and exhaustive input lists in `docs/modules/<module>/functions/`.
- In module docs, describe general concepts before backend-specific or advanced concepts. Within each section, order likely higher-frequency workflows first.
- When a top-level concept doc mentions a public helper, link that helper to its page under that module's `functions/` folder. If a concept describes a workflow centered on a public helper, name and link that helper at least once.
- Function docs live under `docs/modules/<module>/functions/`. `functions/index.md` should start and end with a link back to `../index.md`, group general functions before backend-specific or advanced functions, and order likely high-frequency functions first in each section.
- Each function page should start and end with a link back to `functions/index.md`, then include a brief function description, the exact `function_name(...inputs with defaults...)` signature, input descriptions ordered from most to least frequent, a `## Usage` section, and optional notes. Prefer short public entrypoint pages such as `read.md`, `execute.md`, and `transfer.md` instead of duplicating long-form alias pages such as `read_sql.md`, `execute_sql.md`, or `transfer_table.md`.
- `docs/modules/sql/index.md` should keep the `docs/modules/sql/functions/index.md` link at the top, before concept guides, under the label "All SQL functions".
- In SQL function pages, split `## Inputs` into `### General Inputs` and `### Backend-Specific Inputs` only when both groups are non-empty. If all inputs are general, all inputs are backend-specific, or there are no inputs, keep `## Inputs` as one flat section. Order likely higher-frequency inputs first.
- In SQL function pages, `### Backend-Specific Inputs` should contain only backend-only public inputs with `gp_`, `trino_`, or `ch_` prefixes. Cross-backend table-shape inputs such as `table_schema`, `column_types`, `partition_by`, and `order_by` stay in general inputs.
- Non-SQL function pages should keep `## Inputs` as one flat section without general/backend-specific subsections.
- Usage examples should be concise, use public module imports, and avoid real credentials or production table names.
- At the end of every non-documentation change, run `release_routines/pre_commit_checks.sh` before committing, even if focused tests were run earlier. For documentation-only changes, full checks are not required; run focused tests only when the documentation change affects tested paths or generated artifacts. Treat test failures and pytest warnings as issues to fix before finishing; the final test run should pass with no warning summary.
- Keep `.connections` out of the repo. Tests should create a temporary `.connections` and chdir into that temp project.
- Use existing structured parsers for SQL/table names (`sqlparse`, `sqlglot`) instead of ad hoc parsing where those modules already do the job.
- Once a coherent batch of changes is done, run `git add . && git commit -m '...'`, replacing `...` with a short description of the changes.

## PyPI Release Rules

When the user asks to update, publish, or release the package on PyPI, run the complete publishing workflow unless they explicitly ask for a narrower action:

- Use `release_routines/pypi_release.sh` for the full publishing workflow. It runs TestPyPI publishing and artifact verification first, then real PyPI publishing and artifact verification. Do not call the internal scripts under `release_routines/scripts/` unless the user explicitly asks for a narrower release action or the top-level script itself is blocked.
- If the release only changes documentation or PyPI README content, bump the package version for the release artifact and update `docs/CHANGELOG.md` even though ordinary docs-only changes must not bump versions. PyPI artifacts are immutable, so publishing changed package metadata requires a new version.
- Publish the candidate version to TestPyPI first through GitHub Actions trusted publishing.
- Verify the TestPyPI artifact in a fresh temporary virtual environment from outside the repository checkout, and confirm imports resolve from that environment's `site-packages`.
- Publish the same production package/version to real PyPI through the GitHub release workflow.
- Verify the real PyPI artifact in a fresh temporary virtual environment from outside the repository checkout, and confirm imports resolve from that environment's `site-packages`.
- Check the GitHub Actions jobs after each publish. TestPyPI publishes must leave the real PyPI job skipped; real PyPI publishes must leave the TestPyPI job skipped.
- TestPyPI trusted publishing is currently configured for the temporary project name `karapsin-analytics-toolkit`, while the production PyPI project name is `analytics-toolkit`. For TestPyPI, create a temporary `testpypi-<version>` branch from the exact release candidate commit and change only `[project].name` in `pyproject.toml` to `karapsin-analytics-toolkit`.
- Keep TestPyPI package-name changes on temporary branches only. Do not merge temporary TestPyPI branches into `main`, and publish production PyPI only from the unchanged production project metadata.
- When verifying TestPyPI artifacts, install `karapsin-analytics-toolkit==<version>` but still confirm the import package is `analytics_toolkit` from `site-packages`.
- After every successful deployment to real PyPI and artifact verification, delete all temporary TestPyPI branches locally and remotely, including old `testpypi-*` branches.

## SQL Module Contracts

- Public SQL APIs accept configured DB keys from `.connections`; callers should not need to pass raw backend names or open connection objects.
- Single-DB public SQL functions should name that argument `db_key`.
- Multi-DB public SQL functions may use directional key names such as `from_db`, `to_db`, `source_db`, and `table_db`.
- User-facing SQL imports should use `from analytics_toolkit import sql` or `import analytics_toolkit.sql as sql`. Deep imports under `analytics_toolkit.sql.*` are internal and may change. Do not restore removed root implementation paths.
- Public SQL input names that work only for one backend must use the backend prefix: `gp_`, `trino_`, or `ch_`. Do not keep unprefixed compatibility aliases for those backend-only inputs unless the user explicitly asks for compatibility.
- Each `.connections` value must include `type` as `gp`, `trino`, or `ch`. Backend dispatch comes from this `type`, while reconnect/retry/log messages keep using the alias key.
- Env-based SQL config such as `SQL_CONNECTIONS`, `GP_HOST`, `TRINO_HOST`, `CH_HOST`, `TRINO_INSERT_CHUNK_SIZE`, and config-file override env vars is intentionally unsupported. Do not restore fallback support.
- Keep public directional names such as `from_db`, `to_db`, `source_db`, and `table_db` compatible even when they now represent aliases.
- A Trino target may define `insert_chunk_size` in its connection config. Explicit function arguments override config; config overrides the internal default.
- `sql.read`, `sql.execute`, `load_df`, and `sql.transfer` retry the whole public operation with fresh connections. Preserve Greenplum rollback behavior on errors.
- `transfer` and `load_df` separate key and backend in option models. Same-backend aliases are valid as long as the alias keys differ.
- ClickHouse load/transfer creates and manages a shard table plus a `Distributed` table. Preserve local and cluster DDL/drop/truncate behavior.
- Key validation uses normalized unique key lists and null-safe joins for staged-vs-target overlap checks.
- Trino table metadata helpers need the alias key so unqualified names can use that connection's catalog/schema.

## SQL Layout Notes

- `connection/config.py`: finds `.connections`, parses it as JSON, normalizes aliases to lowercase, validates fields, and resolves alias to backend.
- `connection/get_sql_connection.py`: opens backend clients and handles optional CA certificate files and generated bundles from the `.connections` directory.
- `ddl/create_sql_table.py`: infers dataframe column types, quotes identifiers per backend, and builds ClickHouse distributed DDL.
- `dml/io`: read/execute helpers using `sqlparse`; `read_sql` accepts exactly one statement.
- `dml/load`: dataframe loading, stage table creation, batch insertion, Trino chunking, and backend-specific scalar normalization.
- `dml/table`: shared table existence, analyze, drop, vacuum, stage finalization, and validation helpers.
- `dml/transfer`: staged transfer flow, source streaming, full retry/restart behavior, and connection replacement helpers.

## AB Utilities Contracts

- `compute_test_metrics` expects one row per user, a non-null unique user id, a non-null group column, and at least one mean or ratio metric.
- Output column order is part of the API; preserve placement of `metric_type`, group columns, `p-value CUPED`, and `bootstrap_adj_p`.
- `analytics_toolkit.ab_utils.metrics` re-exports many underscore helpers. Tests may import those names directly.
- Ratio metrics support only `level="agg"` or `level="user"` and `invalid_denominator="ignore"`.
- Missing metric values are ignored per metric/group; non-numeric metric values should raise.
- CUPED failures should warn and return `NaN`, not abort the whole metric computation when validation has passed.
- Bootstrap multiple-comparison adjustment should remain deterministic when `bootstrap_random_state` is set and should fall back from process pools to threads when process pools are unavailable.

## Excel Contracts

- `pivot_and_break_table` and `break_table` accept either one dataframe or a sequence of dataframes.
- Preserve sheet grouping order, table order, side-by-side placement for multiple dataframes, and blank spacing between table blocks.
- Preserve sheet-name sanitization, 31-character truncation, and deduplication for append mode.
- Decimal values are coerced to floats before writing to Excel.
- `enforce_same_row_order=True` aligns later dataframe tables to the first dataframe and rejects extra row labels.

## Dates Contracts

- Date helpers accept ISO strings, `date`, or `datetime` values.
- The default return type is an ISO string; `output_string=False` returns midnight `datetime` values.
- Weekly and monthly sequences truncate start/end dates to the period start and emit warnings when truncation happens.
- `add_weeks` and `add_months` operate from the week/month start, not from the exact input day.

## General Module Contracts

- `time_print` prints timestamped messages and is re-exported through `analytics_toolkit.general` and `analytics_toolkit.sql`.
- `here()` prefers the caller's `__main__.__file__` directory, then falls back to the current working directory and unique cwd matches.
- `read_file()` raises `InvalidSqlInputError` for missing files and applies `str.format(**params_dict)` only when params are provided.
- Preserve the `analytics_toolkit.general.read_file.inspect` compatibility assignment; tests monkeypatch through that dotted path.
