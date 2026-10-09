[Module index](index.md)

# BI projects and existing-dashboard import

[DataLensProject](functions/project.md) manages a recipe and external runtime
state. Use version 2 for named connections, collections, workbooks, source-qualified
fields, joins, RLS, cache invalidation, shared selectors and HTML pages. Legacy
recipes retain their format and public deployment constructor.

## Plan, reconcile and verify

Validate locally first, inspect capability restrictions, then plan the selected
resources. Planning and status share the reconciliation checkpoint and make no
cloud mutations or runtime writes. The write scope includes affected dependents;
the read scope also includes every prerequisite of those resources. An unrelated
chart needed by a rebuilt dashboard is read and retained without being updated.

Resource keys use `connection:`, `dataset:`, `chart:`, `html_page:`, `collection:`,
`workbook:` and `dashboard:main`. Omitted scope selects the full project.
Local changes propose update; remote changes propose pull; simultaneous changes
produce conflicts. Remote drafts and successfully persisted interrupted writes
are distinguished. Removed resources become reported orphans, never deletions.

A pull keeps chart-group member routes and action flags. Changed direct
placements become explicit widgets with the same item IDs, preserving remote
titles, parameter overrides and cross-filter recipients. Unchanged direct
placements retain their original representation.

Verification compares managed recipe content, identities, locations and revision
relationships. It does not execute SQL, evaluate RLS as a user, render charts or
download HTML source. RLS protects dataset access; QL queries bypass datasets.

## Import into the current project

`project.import_dashboard(dashboard_id="source")` previews every tab;
`project.import_tab(dashboard_id="source", tab_id="tab")` previews one tab.
Both return proposed files, a fidelity report, and blockers. Set `dry_run=False`
for a local merge after resolving blockers. CLI commands `import-dashboard` and
`import-tab` use preview by default and `--write` for the merge. Version-1
conversion requires explicit `upgrade_recipe=True` or `--upgrade-recipe`.

Import follows public resource metadata and retrieves available SQL/JS assets.
It preserves IDs, fields, defaults, ordering and placement parameters. Duplicate
persistent resources share one definition; direct placements and chart-group
members keep separate identities. `group/member` wiring addresses one variant.
Existing dataset source and field keys are retained during a merge.
Unsupported item forms, unknown dependency bindings, incompatible resource
locations and conflicting local definitions prevent writes. No dynamic Editor
JavaScript is evaluated to discover dependencies.

New imported inputs remain subject to normal planning, adoption and verification.
Import does not publish or move cloud resources. Existing baselines are preserved.
An incomplete merge is staged and rejected before changes; failed replacements
restore preceding local files.

## Public SDK boundaries

[Capabilities](functions/get-capabilities.md) report SDK availability separately from
adapter support and list operation-specific restrictions. SDK 3.1.0 and 3.2.0
are supported; new installs use 3.2.0. A method missing from either installation
produces an explicit capability error.

Reports, PDF export, scheduled mailings, private embedding, general multi-dataset
Wizard charts, unavailable avatar/topology edits and HTML source retrieval need
additional upstream typed methods. Source retrieval cannot be replaced by an
HTML preview URL. Wizard chart-level parameter overrides are unavailable; place
parameters on dashboard widgets and chart-group members instead.

See the [advanced example](../../../examples/datalens_bi/README.md) for a portable
version-2 recipe. Reuse the installed toolkit interpreter; dashboard projects do
not create environments or install tools.

[Module index](index.md)
