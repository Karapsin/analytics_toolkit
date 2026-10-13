[Module index](index.md)

# Copy an existing tab to a new deployment

Use [DataLensProject](functions/project.md) with a version-2 recipe and an
existing installed SDK environment. Review the [recipe contract](recipe-contract.md)
before editing declarations. Adopt mode retains source IDs and requires compatible
locations; copy mode retains semantic bindings, creates new datasets/charts and a
new destination dashboard, and reuses read-only connections across folders.
Neither import mode changes cloud resources.

## Preview and local merge

Create a destination recipe with empty datasets, chart definitions, layout and
dashboard metadata. Declare the destination folder or workbook in deployment
configuration. Use a separate, unused runtime directory; do not copy source
checkpoints or source persistent IDs. Existing local connection references can
remain. Imports merge with collision checks and reject conflicting definitions.

```python
from analytics_toolkit.datalens_utils import (
    BIProjectDeployment, DataLensProject, TargetLocation,
)

identity = BIProjectDeployment(
    "Copied report", TargetLocation.path("Users/account/copied-report"),
    organization_id="your-organization", token_env="DATALENS_TOKEN",
)
project = DataLensProject("/recipes/copied-report", "/runtime/copied-report", identity)
capabilities = project.capabilities()
preview = project.import_tab(dashboard_id="source-dashboard", tab_id="source-tab", mode="copy")
# {"operation": "import-tab", "mode": "copy", "dry_run": True,
#  "written": False, "provenance": {...}, "proposed_files": {...},
#  "fidelity": [{"resource": ..., "feature": ..., "status": ..., "reason": ...}],
#  "blockers": [...], ...}
```

Review capability restrictions, every dependency ID in provenance, every proposed
file, every normalization and every blocker. Dependency discovery follows Wizard
bindings, selector sources, literal Editor Meta links and dataset connections
across folders. Dynamic Editor scripts are never evaluated. Unavailable authored
source retrieval, unknown bindings and source settings that cannot be reproduced
block local writing. Source dashboards with unpublished drafts also block import.

After a blocker-free preview, repeat the import with `dry_run=False`. The staged
recipe must compile before replacement; failed local replacements roll back.
Inspect the returned result because a repeated import reads the source again.
Copy mode omits persistent IDs from copied objects, sources, avatars and joins.
`copy_from` is provenance, not a retained identity, and prevents later name adoption.

CLI equivalents:

```sh
python dashboard.py import-tab --dashboard-id source-dashboard --tab-id source-tab --mode copy --json
python dashboard.py import-tab --dashboard-id source-dashboard --tab-id source-tab --mode copy --write --json
```

## Validate, plan and deploy

Run `project.validate()` for local schema, references and public builder inputs.
It makes no cloud requests and cannot prove backend schema inference, formula
execution or rendering. Inspect `project.plan()` for writes, dependencies,
capability blockers and remote conflicts. Reconciliation is the separate cloud
write operation; import never implies deployment.

When deployment is authorized, run `project.reconcile()`. It creates source
drafts, matches inferred direct fields by source/avatar and column, checkpoints
actual GUIDs, configures the complete draft and verifies it before publication.
It stops on ambiguous field matches or component errors. Copied chart references
resolve against the new owned dataset identities. Authored Editor Meta retains
unrelated content while managed aliases resolve to copied datasets.

`project.verify(full=True)` then compares the deployed copy against its recipe.
A successful verification cannot recover source settings already omitted by an
importer. Source parity belongs to the import fidelity report. Numerical results
and rendering need separately scoped validation.

## Fidelity checklist

| Feature | Required review |
|---|---|
| Series grouping | All grouping dimensions, color and shape bindings, Measure Names |
| Calculations | Dataset and Wizard formulas, aggregation, local fields, hierarchy order |
| Parameters | Dataset defaults, imported chart defaults on all repeated placements, explicit overrides |
| Filtering | Chart/default filters, RLS metadata, selector influence and ignore edges |
| Identity | Connection reuse, new copied IDs, actual inferred GUIDs, managed Editor aliases |
| Presentation | Titles, formatting, layout, member ordering, action flags |
| Source parity | Observed settings reproduced or explicit blockers |
| Runtime behavior | Backend inference, query results and rendering remain separate checks |

`preserved` records describe extracted/compiled metadata; `normalized` describes
an intentional representation change; `unsupported` identifies a source feature
that blocks writing; `unchecked` identifies work the import did not perform.
The report keeps `features` for compatibility and adds a singular `feature` and
`reason`. Do not infer numerical or rendered equivalence from metadata checks.

Chart defaults become placement defaults; an explicit placement value wins over
the imported chart default. Dashboard globals, selectors and URL state retain
their precedence. Unknown default representations are blockers.

## Failures and recovery

A successful create may persist an ID even when the returned dataset contains
component errors or a later fetch fails. Preserve `resources.json` and the
failure report. `DataLensComponentError.diagnostics` includes the resource, stage,
persistent ID and component errors. Public SDK exceptions retain typed context
and request IDs. The JSON CLI exposes these diagnostics.

Do not reset IDs or retry creation blindly. Correct the recipe or underlying
source issue, inspect status, then resume with the same runtime. Owned drafts
are recovered rather than recreated. No dependent chart/dashboard writes or
publication follow a dataset component failure. Copy destinations with an
existing exact-name resource reject adoption; resolve the name conflict instead
of overwriting that resource. A separate runtime cannot reuse an old deployment
checkpoint.

Cache failures are diagnosed separately from component errors. Verification
compares SQL, both formula forms and filters, without executing the cache query.
Absent cache metadata is equivalent to disabled caching with no filters.
Do not discard a created dataset because a later cache comparison failed.

[Module index](index.md)
