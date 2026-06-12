# Documentation Agent Instructions

Read this file for public documentation work under `docs/` or README
documentation sections.

## Documentation Structure

- When adding, removing, or renaming files under `docs/modules/<module>/`, update `docs/modules/README.md`, that module folder's `index.md`, and any affected function index navigation in the same change.
- `docs/README.md` is the top-level documentation overview. Keep it linked to quick start, the Airflow SQL manual, module documentation, and the changelog.
- All documentation must be reachable through hyperlinks starting from the root `README.md` to `docs/README.md` and then through the relevant overview, module, or function index pages. When adding, removing, or renaming a docs file, update the parent index or overview so the file is discoverable.
- Every documentation Markdown file should include a hyperlink back to the nearest index or overview: top-level `docs/*.md` files link to `docs/README.md`, `docs/modules/README.md` links to `docs/README.md`, module indexes link to `docs/modules/README.md`, top-level module workflow docs link to that module's `index.md`, and function docs link to that module's `functions/index.md`.
- `docs/modules/README.md` should link every module index and should start and end with a link back to `docs/README.md`.
- Every top-level module index at `docs/modules/<module>/index.md` should start and end with a link back to `docs/modules/README.md`.
- Every top-level module index should put the function reference first, before workflow guides, using `## All <Module> Functions`, then `## Workflow Guides`.
- Top-level module docs under `docs/modules/<module>/` should be concept/workflow guides. Keep exact function signatures and exhaustive input lists in `docs/modules/<module>/functions/`.

## Module Documentation Style

- In module docs, describe general concepts before backend-specific or advanced concepts. Within each section, order likely higher-frequency workflows first.
- When a top-level concept doc mentions a public helper, link that helper to its page under that module's `functions/` folder. If a concept describes a workflow centered on a public helper, name and link that helper at least once.
- Function docs live under `docs/modules/<module>/functions/`. `functions/index.md` should start and end with a link back to `../index.md`, group general functions before backend-specific or advanced functions, order likely high-frequency functions first in each section, and include a short lowercase description next to each function hyperlink separated with ` - ` and no trailing period.
- Each function page should start and end with a link back to `functions/index.md`, then include a brief function description, the exact `function_name(...inputs with defaults...)` signature, input descriptions ordered from most to least frequent, a `## Usage` section, and optional notes. In `## Inputs` sections, separate each backticked input name from its description with ` - `, start the description lowercase, and omit trailing periods. Prefer short public entrypoint pages such as `read.md`, `execute.md`, and `transfer.md` instead of duplicating long-form alias pages such as `read_sql.md`, `execute_sql.md`, or `transfer_table.md`.
- `docs/modules/sql/index.md` should keep the `docs/modules/sql/functions/index.md` link at the top, before concept guides, under the label "All SQL functions".
- In SQL function pages, split `## Inputs` into `### General Inputs` and `### Backend-Specific Inputs` only when both groups are non-empty. If all inputs are general, all inputs are backend-specific, or there are no inputs, keep `## Inputs` as one flat section. Order likely higher-frequency inputs first.
- In SQL function pages, `### Backend-Specific Inputs` should contain only backend-only public inputs with `gp_`, `trino_`, or `ch_` prefixes. Cross-backend table-shape inputs such as `table_schema`, `column_types`, `partition_by`, and `order_by` stay in general inputs.
- Non-SQL function pages should keep `## Inputs` as one flat section without general/backend-specific subsections.
- Usage examples should be concise, use public module imports, and avoid real credentials or production table names. Every `## Usage` section should include an output example that shows the expected return shape, printed output, or generated SQL/plan excerpt.
