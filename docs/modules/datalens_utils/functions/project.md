[Functions index](index.md)

# DataLensProject

`DataLensProject(project_root, runtime_root, deployment, *, client_factory=None, reporter=None)`

## Inputs

- `project_root` - recipe directory containing configs and assets
- `runtime_root` - external directory for checkpoints and exports
- `deployment` - Deployment or BIProjectDeployment identity for YC or Enterprise
- `client_factory` - optional callable accepting a Session and returning a client context manager
- `reporter` - optional print-compatible callback; omitted callers remain silent

## Usage

```python
from analytics_toolkit.datalens_utils import DataLensProject, Deployment

identity = Deployment("org", "Users/account/sales", "Sales", "ClickHouse", "connection", "profile")
project = DataLensProject("/project/recipe", "/project/runtime", identity)
report = project.validate()
# {"operation": "validate", "coverage": {...}, "request_count": 0,
#  "elapsed_seconds": ..., "messages": [...]}
```

`validate()` checks local inputs without cloud requests or runtime writes.
`reconcile()` creates or recovers resources and verifies/exports their metadata.
`status()` inventories revisions without cloud writes.
`pull(*, chart_keys=None, dataset_keys=None, ui=False, resource_keys=None, branch="published")`
imports supported changes with a three-way merge; saved explicitly imports drafts.
`apply(*, chart_keys=None, dataset_keys=None, ui=False, resource_keys=None)` applies affected changes.
Choose one scope at a time. `verify(*, full=True)` supports full published metadata.
`session(*, reporter=None)` binds isolated configuration for adapter calls.
Reports include operation, request_count, elapsed_seconds and messages; status
also includes resource_statuses. Typed SDK errors are preserved.

`capabilities()` returns the installed operation matrix. `plan(*, resource_keys=None)`
returns actions, read/write scopes, conflicts and capability blockers.
`import_tab(*, dashboard_id, tab_id, dry_run=True, upgrade_recipe=False)` and
`import_dashboard(*, dashboard_id, dry_run=True, upgrade_recipe=False)` preview or
merge published content. Reports include proposed_files, fidelity, blockers and
written. Preview makes no local writes; both make no cloud mutations.

[Functions index](index.md)
