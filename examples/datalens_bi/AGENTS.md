# Dashboard project rules

Keep dashboard.py thin and use analytics_toolkit.datalens_utils for the engine.
Assume analytics-toolkit[all] is already installed. Reuse the current Python
environment and existing YC CLI; do not create a .venv, install or bootstrap.
Edit deployment constants, JSON and assets first. Keep stable semantic keys and
local GUIDs. Do not place generated runtime, credentials or private exports
inside this recipe. Runtime state must remain external.

Use the installed SDK skill and public API, with its exact resolved interpreter
and preflight. Reuse an existing connection/profile; preparation is separate.
Validate names/configuration before writing. Preserve checkpoints and IDs for
updates. Never repeat a persisted create because its subsequent fetch failed.

Use API and code checks; no browser automation is needed by this template.
Metadata/fixtures are separate from numerical correctness. Obtain permitted
result handling before queries. Use the analytics-toolkit skill and its public
sql.read in the existing external interpreter for independent SQL tests.
Every test SQL must contain actual table names and a first-line chart/selector
comment and be directly runnable. Scenario filters use a comment anchor.
Always batch DataLens API queries wherever public operations support it. Gather
IDs for public bulk/paginated reads, consolidate compatible dataset measures at
identical grain/filters, and cache exact repeats. Share pacing across clients in
one organization when operations lack a public batch method. Concurrency and
caching are not API batches; do not use private endpoints to bypass SDK limits.
Record actual results, revisions, tolerances and limitations in VERIFICATION.md.

Version-2 scaffolds also support YC/Enterprise and folder/workbook targets.
Use capability and plan reports before writes. `import-dashboard` previews all
tabs; `import-tab` previews one. Resolve fidelity blockers before `--write`.
Reuse the installed toolkit environment; do not create environments or bootstrap.

This advanced recipe uses two sources and an explicit calendar join, source-qualified
field GUIDs, a dataset parameter, managed example RLS, cache invalidation off,
parameterized chart-group variants with distinct selector routes, and local HTML.
The calendar table is an external prerequisite; validation executes no queries.
The retained financial dashboard is unrelated to this synthetic example.
