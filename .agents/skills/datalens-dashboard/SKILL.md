---
name: datalens-dashboard
description: Create or maintain configuration-driven Yandex DataLens dashboard projects using analytics_toolkit.datalens_utils. Use for scaffolding, recipes, selectors, incremental updates, recovery, and API verification.
---

# DataLens dashboard projects

Create a shareable recipe with a thin `dashboard.py`, persistent-object JSON,
separate UI JSON, SQL/JavaScript assets, independent SQL tests and offline
checks. Keep the engine in `analytics_toolkit.datalens_utils`. Follow the
requested business scope and maintain coverage for the actual declared objects.

## Start or adapt

Read [project structure](references/project-structure.md) when creating a
project or changing ownership. Use the self-contained starter:

```sh
python scripts/scaffold.py /path/to/new-project \
  --table your_database.your_table \
  --organization-id YOUR_ORG --yc-profile YOUR_PROFILE \
  --target-path Users/your-account/dashboard-folder \
  --dashboard-name 'Sales dashboard' \
  --connection-name 'Existing ClickHouse' --connection-id YOUR_CONNECTION
```

Resolve the script relative to this skill. It refuses to overwrite a project,
copies portable assets and writes actual table names into independent SQL. It
does not install dependencies, log in, query data or deploy. Its small example
demonstrates all three families; retain the families the user needs. Wizard is
the usual choice for dataset-backed charts; use QL/Editor for a concrete need or
explicit choice.

Assume `analytics-toolkit[all]` and its dependencies are already installed in
an existing Python 3.10+ environment. Use that interpreter for the launcher,
SDK preflight, offline tests and SQL checks. Do not create a dashboard `.venv`,
install dependencies, pin a separate Python, generate a lockfile, stage an
engine source checkout or invoke bootstrap. Import the separate module as
`analytics_toolkit.datalens_utils`. If the environment lacks a prerequisite,
report the missing prerequisite and reuse a suitable existing environment.

Adapt deployment constants, dataset roles, explicit projections, calculations,
chart definitions, recipients and layout before writes. Keep semantic keys and
local field GUIDs stable during edits. A new deployment needs separate runtime
state and deliberate persistent-ID reset.

For an existing dashboard migration, preserve IDs, deployment constants, SQL
and all recipe JSONs. Bind helper calls through the new project's session.
Compare public SDK exports from the previous and shared engines using offline
fixtures and deterministic generated IDs before publishing. Model existing
layout through a saved snapshot when it contains tolerated warnings. Document
any normalization and keep business IDs, fields, ordering, filters and parameters
in the comparison. Update every project Markdown file, including `AGENTS.md`
and dated verification evidence. Reuse the existing CLI; do not copy tools.

Business tabs, chart groups, dataset parameters, selector dependencies and
managed aliases are described in [project structure](references/project-structure.md).

## Runtime and SDK

Pass project root, external runtime and `Deployment` explicitly to
`DataLensProject`. The launcher uses the installed module directly.
Base imports and construction must not install tools, obtain credentials or
read recipes. Checkpoints and exports belong outside the project. Use the existing YC CLI
and profile; `DATALENS_YC_BIN` can select an existing executable.

Locate the installed SDK skill with `datalens_sdk.agent_skill_paths()` using
the resolved interpreter. Read its `SKILL.md` and relevant factory, parameter
and lifecycle references before SDK work. Run its absolute preflight script
from the dashboard project with that exact interpreter. Use typed public
factories/getters/builders and installation capabilities; do not replace an
absent method with raw HTTP, private imports or invented payloads.

**Always batch DataLens API queries when the public API supports the operation.**
Collect IDs before public bulk/paginated inventory reads. Combine compatible
dataset columns/measures only at identical grain, filters and parameter state,
and cache exact repeats within the verified revision snapshot. When no public
batch method exists, use typed individual calls with shared pacing across
clients accessing the same organization. Concurrent calls and caching are not
API batches. Do not invent a private batch endpoint or increase concurrency to
push through a quota.

Reuse existing connections and profiles. Credentials stay external; source
preparation is separate. Persistent resource IDs are ordinary configuration.
Respect the installed SDK's result-handling requirement before data queries;
use the user's authorized handling rather than assuming permission.

## Edit and verify

Use `dashboard.py validate` for local inputs, no command for creation/recovery,
`status` for revisions, scoped `apply`/`pull` for edits and `verify --full` for
published metadata. Read [recovery](references/recovery.md) for drafts, moves,
conflicts and failed writes, and [verification](references/verification.md) for
data comparisons.

Keep fixture, metadata, numerical and rendering claims distinct. The default
is API and code verification; do not add browser automation to imitate an SDK
data method. Respect an explicit different scope in a future project.

Report the dashboard URL, identities, performed checks and concrete gaps. A
SQL/network failure is blocked numerical verification. Preserve successful
writes and checkpoints. After SDK retries are exhausted, stop that run and
report the typed failure and request ID instead of adding a rapid retry loop.
