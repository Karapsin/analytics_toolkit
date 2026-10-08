# Structure and configuration

The portable starter is in `assets/project/`, without company IDs, credentials,
copied engine or project-local `utils`.

| Location | Ownership |
| --- | --- |
| `dashboard.py` | Organization/profile, target/name, connection; explicit launcher |
| `configs/runtime.json`, `coverage.json` | YC/pacing and finite versioned coverage contract |
| `configs/DL objects/dashboard.json` | Description, presentation, subfolders and retained ID |
| `configs/DL objects/datasets.json` | Roles, actual tables, projections, fields/formulas and IDs |
| `configs/DL objects/charts/<family>/<type>/*.json` | One persistent chart per file |
| `configs/DL objects/selectors/editor/selector/` | Persistent Editor selectors |
| `configs/UI/` | Tabs, text, layout, selectors/groups and wiring |
| `assets/sql/`, `assets/js/` | Deployed SQL and complete Editor tabs |
| `sql_tests/`, `tests/` | Independent runnable SQL/scenarios and offline checks |
| `AGENTS.md`, `VERIFICATION.md` | Local rules and dated evidence |

Charts can set an explicit `tab` business key; without it the family key
(`wizard`, `ql`, `editor`) remains the default. Multiple business tabs can use
the same chart family. `UI/chart_groups.json` defines ordered charts within a
single widget: set each member's `key`, `title`, optional `params`, and exactly
one `default: true`; position the group key in layout. Every chart is placed
once, either directly or inside a group. `show_title: false` hides the internal
Wizard title while retaining its text during import.

Runtime defaults to sibling `.local/<project-name-without-spaces>/`, overridden
by `DATALENS_RUNTIME_DIR`. It must remain external, including through symlinks.
Use the already installed `analytics-toolkit[all]` environment; do not create
a dashboard virtual environment or bootstrap tools. Deployment constants belong only in the launcher. Never infer recipe paths from
installed package location.

Use ordinary `database.table` identifiers and explicit CH_SUBSELECT projections.
Project-relative SQL/JS references must remain within the recipe. Raw numeric
facts stay dimensions; measures use formulas such as `SUM([Revenue Raw])` with
formula aggregation `none`. Ratios divide totals with zero/null handling.
Wide integers and precision-sensitive decimals may need string companions;
that projection does not establish native storage-type coverage.

Chart references use human field titles, resolved to real public field handles.
Keep local GUIDs stable. Map variants use `wizard/geolayer/<layer-type>/`;
Gravity variants use `editor/gravity_charts/<series-or-variant>/`.

Layout owns `[x,y,w,h]` on the 36-column grid. Every declared item has exactly
one position. Keys are unique throughout the dashboard. Standalone native
selectors use `<key>_control` wrappers; Editor placements use their own keys.
Persistent names reject `·`, `×` and `/`, even though presentation titles can
use them. Validate the pinned United Storage grammar before writes; do not
silently sanitize names.

Dataset selectors bind fields; cross-dataset aliases are explicit. Manual selectors bind declared QL/Editor parameters or Wizard dataset
`parameters` (each has `type` and `default`). Dataset `default_filters` configure
field-value queries; explicit chart filters still govern chart results.
Selectors may receive other selectors, enabling dependent date choices.
Use explicit aliases with `{parameter: name}` or `{dataset: role, field: title}`
for parameter-to-field binding. Checkpoints track managed aliases so user
aliases remain intact. Only previously managed obsolete tabs are retired.
`preserve_layout: true` on an existing business tab tolerates its current
layout warnings; new layouts still require non-overlapping geometry. Editor uses array-of-string parameters and
actual Sources conditions. Matching labels do not bind these mechanisms.
`UI/links/connections.json` is a receiver allowlist; the compiler writes ignore
edges for its complement. SDK `add_connection(from_item=receiver,
to_item=sender)` means the receiver ignores the sender. Preserve that direction.

QL multiselect predicates use `IN {{parameter}}`; the formatter adds tuple
parentheses. SDK 3.1.0 QL column casts are only `string`, `integer` and
`genericdatetime`; do not copy dataset `date`/`float` casts into QL JSON.
QL has no sort/format setters; express ordering in its SQL.
Optional "only" checkboxes restrict only when checked; a boolean
dataset selector selects a cohort. Editor selectors cannot become native
group members or pinned shared filters by copying visible controls.

Independent SQL contains actual tables and runs as written. The
`-- SELECTOR_FILTERS` comment lets the runner insert `AND` predicates. Keep the
first-line comment naming charts and selectors/controls. Update concrete SQL
names with dataset sources and reject source mismatches.
