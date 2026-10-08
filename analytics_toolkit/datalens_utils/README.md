# DataLens Utils

Reusable YC dashboard creation, configuration validation, incremental editing,
durable recovery, and metadata verification. The engine
uses the public DataLens SDK. Dashboard recipes and runtime state belong to the
calling project.

Install the optional engine dependencies on Python 3.10 or newer:

```sh
pip install 'analytics-toolkit[datalens]'
```

Base imports support Python 3.8 through 3.14 and do not import the SDK, read a
recipe, install tools, obtain credentials, or create files. SDK operations are
pinned to `datalens-sdk==3.1.0`. The caller can supply an existing YC CLI, or
explicitly use the optional bootstrap helpers for project-owned tools.

```python
from analytics_toolkit.datalens_utils import DataLensProject, Deployment

deployment = Deployment(
    organization_id="your-organization",
    target_path="Users/your-account/your-folder",
    dashboard_name="Sales dashboard",
    connection_name="Existing ClickHouse",
    connection_id="your-connection",
    yc_profile="your-profile",
)
project = DataLensProject("/path/to/recipe", "/path/to/external-runtime", deployment)
project.validate()
report = project.reconcile()
```

`validate()` checks local inputs without cloud requests. `reconcile()` performs
the full creation/recovery workflow, verifies published metadata, exports it
externally, and records persistent IDs. `status()` returns resource statuses.
`pull(chart_keys=[...], branch="published")` imports remote changes with a
three-way merge; `branch="saved"` explicitly imports a draft.
`apply(chart_keys=[...])` applies affected local changes. Both also accept
`dataset_keys=[...]` or `ui=True`; these scopes are mutually exclusive.
`verify(full=True)` checks full published metadata and references.

The CLI also accepts `validate`, which checks local configuration without cloud
requests. With no command it performs full reconciliation; `verify --full`
verifies published metadata; `apply` and `pull` accept `--chart`, `--dataset`,
or `--ui`.

Operation reports are dictionaries with `operation`, `request_count`,
`elapsed_seconds`, and `messages`, plus operation-specific IDs, coverage, or
resource statuses. Library operations are silent unless a `reporter` callback
is supplied. `analytics_toolkit.datalens_utils.cli.run_cli(project, argv)` preserves
commands and console output, returning an exit code. Library failures raise
`DataLensUtilsError` subclasses; public SDK errors retain their types.

For independent numerical checks, adapters can be used within an explicit
`with project.session():` scope. Each scope reads the current runtime JSON and
owns its request counter and configuration. Context-local binding isolates
nested and concurrent projects without changing module globals. A custom
`client_factory(session)` can supply a context manager for offline SDK clients.

Recipe ownership remains unchanged: deployment constants in the launcher;
runtime settings and coverage under `configs/`; one JSON per persistent chart
or Editor selector; UI layout and wiring separate from objects; SQL/JS assets
inside the recipe root. Routine changes should edit these inputs.

Always batch DataLens API queries whenever the public API supports the operation.
The engine collects revision IDs for public bulk/paginated inventory reads.
Collect IDs first instead of performing per-resource revision calls. Compatible
dataset columns/measures may share a query when grain, filters and parameters
are identical; preserve semantics and cache exact repeats. Distinct queries
without a public batch method remain typed individual calls. Consumers using
multiple clients for the same organization must share request pacing. Caching
and concurrency are separate from API batching; private endpoints are excluded.

The engine does not prepare SQL source tables or execute deployed charts by ID.
Metadata verification is distinct from numerical results and browser behavior.
The dashboard skill supplies independent SQL scenarios and an API/code comparison harness.

Dashboard projects assume analytics-toolkit[all] is already installed and reuse
the current interpreter and existing YC CLI/profile. The dashboard skill never
creates an environment or invokes bootstrap. The legacy bootstrap submodule
remains an explicit opt-in utility for callers who need it; imports and facade
operations never invoke it. Runtime defaults to an external sibling
.local/<project-name-without-spaces>, overridable with DATALENS_RUNTIME_DIR.

Public facade exports: DataLensProject, Deployment, ProjectPaths,
DataLensUtilsError, DataLensConfigurationError, DataLensDependencyError.

Offline tests use temporary recipes and public SDK clients with MockTransport.
They do not authenticate or access cloud dashboards or databases.
