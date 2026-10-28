# Version-2 BI recipes and import

Use `schema_version: 2` for named connections and containers, workbook targets,
qualified dataset sources, joins, RLS, cache invalidation and HTML pages. Use
`BIProjectDeployment` and `TargetLocation`; keep legacy deployment constructors
for existing projects. SDK 3.1.0 and 3.2.0 are tested; new installations use 3.2.0.

Resolve installation and connector support with `project.capabilities()`. Each
operation separates SDK availability from adapter restrictions. Never replace a
missing public method with HTTP, private imports or invented data/meta payloads.

Use `plan(resource_keys=[...])` before scoped updates. It includes prerequisites
of affected resources without silently expanding the write scope. Retain IDs,
remote draft protection and owned-write recovery. Verification must compare
managed content, including formulas and selector defaults, as well as revisions.

For a new local project, use the [package import command](import-dashboard.md).
For a merge into an existing project, use its `import_dashboard` for every tab or `import_tab` for one
selected tab. These methods share the new-project capture pipeline, automatically
resume exact-revision cached reads, and check returned and final published
revisions. Preview preserves immutable source provenance under the proposed
`configs/imports/<capture-id>/` files; writing commits it with the merged recipe
without changing the destination dashboard identity. Preview is the default; merge into the current project only after
reviewing blockers, collisions and the fidelity report. Import changes no cloud
resources. SQL/JS extraction is restricted to source exposed by public getters;
dynamic dependency discovery must not execute scripts. HTML authored source is
unavailable. Version-1 conversion requires the explicit upgrade option.

SDK 3.1/3.2 layout builders accept integer grid coordinates. Whole-number floats
can be converted exactly in memory; preserve authored recipe files. For
unsupported fractional coordinates, round automatically as described in the
skill, record each change in the fidelity report, and check bounds and overlaps.
No confirmation is required. Never use private payload edits to bypass the typed
layout API.

Use named UI widget placements for repeated chart references. Chart-group members
have their own stable keys and may reference a separate `chart` resource. Wiring
`group/member` retains individual variant identities. Place Wizard parameter
overrides on widgets and group members; chart-level Wizard params must fail
explicitly. Shared selectors distinguish display from influence and need geometry
on every displayed tab. Cascading selectors require dependent selectors.

The scaffold supports `--schema-version 2`, installation/base URL, workbook ID or
key, connector, public source factory and JSON source parameters. The generated
launcher uses the installed toolkit environment. Do not create `.venv`, install
dependencies or bootstrap tools in a dashboard project.

See the toolkit's public BI workflow and `examples/datalens_bi` for the complete
configuration contract and upstream requirements for blocked operations.

For copying, use `mode="copy"` (CLI `--mode copy`), a destination recipe without
copied IDs, and a separate unused runtime. Connections are reused; datasets,
charts and the destination dashboard get new IDs. Adopt mode is the default and
retains each resource's original location. `copy_from` prevents accidental name adoption.
Copied Editor Meta uses managed semantic aliases while retaining unrelated authored
content. Chart defaults are normalized onto every repeated placement.

Read the public [copy-tab walkthrough](https://github.com/Karapsin/analytics_toolkit/blob/dev/docs/modules/datalens_utils/copy-tab.md)
and [recipe contract](https://github.com/Karapsin/analytics_toolkit/blob/dev/docs/modules/datalens_utils/recipe-contract.md)
for exact inputs, fidelity statuses, precedence and recovery boundaries. These
contracts should resolve ordinary consumer work; package source is a debugging
fallback. Match the documentation branch/version to the installed toolkit.
