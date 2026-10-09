# API and numerical verification

Local recipe checks and synthetic fixture/Node tests establish configuration,
serialization and code behavior. Re-fetch all managed published objects to
verify fields, formulas, tabs, bindings, layout, references and exact paths.
Neither establishes business values or browser behavior.

Always batch DataLens API queries wherever public operations support batching.
Gather all IDs for bulk/paginated metadata inventory, including both sides of a
comparison when their organization matches. Compatible measures can share a
dataset query only when dimensions/grain, filters and parameter state match.
Cache identical requests without conflating caching with API batching. Distinct
queries without a public batch method need serialized, shared pacing across
clients. After SDK retries exhaust a 429, preserve completed cases, allow the
quota to reset and make one deliberate retry at reduced request volume.

Before data reads, obtain result-handling permission as required by the installed
SDK. The starter runner analyzes rows transiently and saves only scenario,
revision and tolerance evidence externally; run it only when that handling is
authorized. Do not print raw values unless permitted.

Use the analytics-toolkit skill when available, public `analytics_toolkit.sql.read`
and an existing external interpreter/connection alias. Preserve toolkit
defaults. Do not construct another backend client or copy private connection
files. Independent SQL stays separate from deployed queries, with concrete
table names for copy/paste use.

| Family | Comparison | Boundary |
| --- | --- | --- |
| Wizard | Dataset query at chart grouping grain and selector-equivalent filters versus SQL | Does not execute chart-local window/rank formulas |
| QL | Published SQL bound with pinned formatter semantics versus independent SQL | Does not execute deployed chart runtime |
| Editor | Published Sources/Prepare in Node with actual dataset rows versus SQL | Does not execute browser events/rendering |

SDK 3.1.0 exposes dataset data, not `chart.get_data(params)`. Getters return
configuration; update `.execute()` persists changes. Do not replace the absent
chart-data method with private routes.

Cover cleared/default, single/multiple, empty matches, date boundaries,
checkbox states, thresholds, search, reset/preset, aliases and unaffected
receivers as applicable. Resolve recipients from published wiring and
allowlists. Group apply/reset checks prove resulting parameter states and
definitions, not clicks. Presentation controls need no-filter assertions.

Compare dimension identities, multiplicities and numbers with reported
tolerances. Record revisions before/after and reject changes or row-limit
truncation. Keep raw rows transient unless other handling was authorized.

Unavailable SQL means blocked independent comparisons with zero evaluated
cases. Authorized paired dataset/Editor checks can establish migration parity
but do not replace independent SQL. A QL definition comparison is not SQL
execution. Never label fixture or metadata success a numerical pass.

Write dated `VERIFICATION.md` with actual counts, identities, tests, blocked
conditions and precise capability gaps.

For named BI resources and tab/whole-dashboard imports, see [BI projects](bi-projects.md).
