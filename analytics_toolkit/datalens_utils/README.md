# DataLens Utils

Reusable YC dashboard creation, configuration validation, incremental editing,
durable recovery, and metadata verification. The engine
uses the public DataLens SDK. Dashboard recipes and runtime state belong to the
calling project.

Managed selector ignore edges use stable source/target ordering across supported
Python versions and hash seeds.

Install the optional engine dependencies on Python 3.10 or newer:

```sh
pip install 'analytics-toolkit[datalens]'
```

Base imports support Python 3.8 through 3.14 and do not import the SDK, read a
recipe, install tools, obtain credentials, or create files. SDK operations are
compatible with `datalens-sdk` 3.1.0 and 3.2.0. New installations use 3.2.0.
Dashboard projects reuse the existing interpreter and tools.

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

Charts support explicit business `tab` keys, defaulting to their family.
`configs/UI/chart_groups.json` places ordered chart tabs within one widget,
including per-chart defaults and parameters. Wizard `show_title` preserves
hidden title settings through configuration and import. Dataset `parameters`
use `type` and `default`; `default_filters` configure selector value queries.
Manual Wizard selectors can drive these parameters and explicit parameter/field
aliases. Selector recipients may include other selectors; the engine enables
dependent selectors when needed. Managed alias checkpoints preserve user aliases.
Only previously managed obsolete tabs are retired; existing `preserve_layout`
tabs tolerate their current warnings. Source updates preserve owned dataset and
field identities and require configured IDs to match checkpoint ownership.

## Version-2 BI projects

Version-2 pulls preserve chart-group member routes and action flags. A changed
direct placement becomes an explicit widget with the same item ID, retaining
its remote title, parameters and cross-filter recipients; unchanged placements
keep their existing representation.

SDK operations accept exactly 3.1.0 and 3.2.0; new installations use 3.2.0.
Use `schema_version: 2` in `configs/runtime.json` for named BI resources.
The existing recipe format remains supported without implicit file conversion.

```python
from analytics_toolkit.datalens_utils import (
    BIProjectDeployment, DataLensProject, TargetLocation, get_capabilities,
)

identity = BIProjectDeployment(
    "Sales", TargetLocation.workbook(key="team"),
    organization_id="your-organization", token_env="DATALENS_TOKEN",
)
project = DataLensProject("/project/recipe", "/project/runtime", identity)
capabilities = get_capabilities(installation="yc")
preview = project.plan(resource_keys=["chart:trend"])
# {"actions": [...], "write_scope": [...], "read_dependencies": [...],
#  "conflicts": [...], "capability_blockers": [...], ...}
project.apply(resource_keys=["chart:trend"])
```

`TargetLocation.path(path)` targets a folder; `workbook(by_id=...)` uses an
existing workbook, while `workbook(key=...)` references its recipe declaration.
Changing between these storage models is a migration blocker. Enterprise uses
`installation="enterprise"` and `base_url`; `token_env` optionally names an
OAuth token. YC retains existing CLI/profile authentication or uses `token_env`.
Injected client factories remain supported.

Named connections, collections, workbooks and HTML pages live in their respective
JSON mappings beneath `configs/DL objects/`. Existing connections are read-only
references. Managed connections declare a connector, nonsecret parameters,
environment secret references and a nonsecret `credentials_revision` marker.
Container parents reference collection keys. Root container name adoption is
unavailable without a retained ID; nested adoption requires an exact unique name.

Dataset sources declare connection keys, public source factories and parameters;
SQL sources reference contained assets. Direct fields name their source and
column. Join creation uses explicit sources; ambiguous duplicate-column updates
and unavailable avatar topology changes fail before persistence. Stable RLS keys
own field/subject pairs and preserve unrelated rules. RLS verification checks
persisted metadata; QL queries bypass dataset RLS. Cache modes are SQL, formula
(with both formula representations), and off; verification executes no queries.

Shared selectors distinguish display (`show_on_tabs`) from influence (`affects`).
Every displayed tab supplies layout geometry. Wiring retains individual chart
variants: `group/member` identifies one chart-group member; a group reference
addresses all its members. Members may declare `chart` separately from their
stable `key`, allowing repeated parameterized placements of the same chart.
Optional `configs/UI/widgets.json` provides named direct placements. Direct
widgets and group members expose `enable_action_params`, defaulting to false.
Cascading selectors require dependent selectors. Settings preserve omitted
values; explicit `null` resets supported settings or removes a global parameter.

Wizard local formulas, aggregated measures and hierarchies require stable GUIDs.
Wizard chart-level `params` are unsupported; use dashboard placement overrides.
HTML uploads use contained UTF-8 assets and preserve revisions and source
fingerprints. Getters expose metadata, so remote HTML source pull/import is
unavailable and remote changes block automatic overwrite.

## Import a dashboard or tab

```python
preview = project.import_dashboard(dashboard_id="source-dashboard")
# {"dry_run": True, "written": False, "proposed_files": {...},
#  "fidelity": [...], "blockers": [...], ...}
preview = project.import_tab(dashboard_id="source-dashboard", tab_id="business-tab")
result = project.import_dashboard(dashboard_id="source-dashboard", dry_run=False)
```

Imports merge into the current project and change no cloud resources. Preview is
the default. Import discovers dependencies, extracts available SQL/JS assets,
retains IDs and placement parameters, and reports collisions and unsupported
features. Local writing requires a blocker-free staged recipe; failed
replacements roll back. Version-1 projects need explicit `upgrade_recipe=True`.
Unresolved dynamic Editor dependencies, incompatible locations, unknown item
forms and unavailable authored source retrieval block writes.

CLI equivalents include `capabilities`, `plan`, `reconcile`, repeatable
`--resource KIND:KEY` for plan/apply/pull, and `--json` for one structured result.
Use `import-dashboard --dashboard-id ID` or
`import-tab --dashboard-id ID --tab-id TAB`; add `--write` for the local merge.

Capability reports separate SDK and adapter support, with reasons, restrictions
and prerequisites. Reports need typed lifecycle/revision/verification methods;
PDF needs an export job/result and retrieval contract; mailings need schedules,
recipients and status; private embedding needs key/embed lifecycle and token
contracts. General multi-dataset Wizard charts need registration, links and exact
field resolution. Join topology edits need explicit avatar handling. HTML pull
needs authored-source retrieval. These features are not implemented through
HTTP fallbacks or private SDK imports.

See the [advanced BI workflow](../../docs/modules/datalens_utils/bi-projects.md)
and its portable example for full recipe details.
