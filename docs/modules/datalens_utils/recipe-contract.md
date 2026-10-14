[Module index](index.md)

# Version-2 recipe contract

Use this contract with [DataLensProject](functions/project.md) and SDK 3.1.0 or
3.2.0. Set `schema_version: 2` in `configs/runtime.json`. Recipes contain
configuration and contained SQL/JavaScript assets; checkpoints, exports and
persistent backend ownership belong to the separate runtime directory.
Unknown resource properties are errors. Object mappings use stable semantic
keys; persistent `id` values are optional strings for adoption, never names.

## Datasets and connections

`configs/DL objects/connections.json` maps keys to connection objects. Existing
connections require `mode: "existing"`, `id`, `name`, and `connector`; they are
read-only references and can be reused outside the destination folder.
Managed connections use `mode: "managed"`, nonsecret `parameters`, environment
variable names in `secrets`, and `credentials_revision` when secrets are declared.
`description` and `target` are optional. Credentials never belong in recipes.

`configs/DL objects/datasets.json` maps keys to dataset objects:

| Member | Type and behavior |
|---|---|
| `name` | Required nonempty string |
| `id` | Optional retained persistent ID; omit for creation |
| `copy_from` | Optional provenance ID; forbids name adoption when no owned ID exists |
| `description` | Optional string; omit empty imported descriptions |
| `sources` | Required mapping of source keys to factory declarations |
| `fields` | Mapping of field keys to direct source-qualified fields |
| `calculations` | Mapping of field titles to formula definitions |
| `parameters` | Mapping of parameter names to definitions |
| `relations` | Mapping of stable relation keys to joins |
| `default_filters` | List of field/operation/value declarations |
| `rls` | Mapping of stable rule keys to field/subject declarations |
| `cache_invalidation` | Optional cache declaration below |
| `settings` | Mapping of public SDK setting names to values |
| `aggregations` | Optional mapping of field names to aggregation strings |

A source has `connection` (semantic key), `factory` (public SDK source factory),
`parameters` (JSON object), optional `sql_file`, and optional retained `id` and
`avatar_id`. Factory arguments must match the installed public factory signature.
For `ch_table`, use `db_name` and `table_name`; for `ch_subselect`, use `sql_file`
instead of inline `subsql`. The importer removes the API-only `manual` flag;
unknown constructor arguments block import rather than disappearing silently.

A direct field requires `source` and `column`. Optional properties are `guid`,
`title` (defaults to the mapping key), `cast`, `kind`, `aggregation` (defaults to
`none`), `hidden`, `description`, and `ui_settings` (a public SDK field-formatting JSON string). Kind defaults to `DIMENSION` for `none`,
otherwise `MEASURE`. Imported API descriptions that are empty are omitted.
Backend-inferred GUIDs are checkpointed; original recipe GUIDs map to those
owned identities without adding duplicate fields. Version-2 pulls normalize cloud
GUIDs back to declared recipe GUIDs before merging metadata and storing baselines.
This includes cache `guid_formula` references, Wizard aggregate source fields and
dataset-backed hierarchy members; chart-local GUIDs and member order stay intact.
Scoped pulls leave unselected recipe files and checkpoint entries unchanged.
Synthetic local validation uses recipe GUIDs, while cloud builders resolve them
through an ID-matched checkpoint only when the inferred field exists.
Distinct-title clones of the
same source column retain separate identities; an inferred column is claimed only
once. Formula/parameter visibility, descriptions and nonempty formatting that the
recipe cannot represent block import.

Calculations require `formula`, with optional `guid`, `cast`, `kind`, and
`aggregation`. Parameters require `type` and `default`, with optional `guid`.
Parameter names are case-sensitive receiving keys. Supported types are the
public SDK parameter types: string, integer, float, date, datetime, boolean.

Relations require `left` and `right` source keys, `type` (`inner`, `left`, `right`,
`full`), and `conditions`: a list of `{left, right, operator}` using source column
names and SDK comparison operators. `id` and `drop_duplicates` are optional.
Unknown or ambiguous avatar topology is blocked.

Default filters use `{field, operator, values}`; `field` is a recipe field key.
RLS rules use `field`, `subject_id`, `subject_type`, optional `subject_name`,
`allowed_value`, and `pattern_type` (defaults to `value`). RLS protects dataset
access; QL bypasses datasets.

Cache declarations use `mode: "off"|"sql"|"formula"`. SQL requires `sql_file`.
Formula requires `field: {guid, type, formula: {formula, guid_formula}}`;
`type` defaults to `MEASURE`. Both formula representations must be supplied.
Optional `filters` contain `{key, field, operation, values}`. An absent backend
cache source and an explicit off source with no filters are equivalent.
Verification checks metadata and never runs the cache query.

```json
{
  "sales": {
    "name": "Sales",
    "sources": {"facts": {"connection": "ch", "factory": "ch_subselect", "sql_file": "assets/sql/sales.sql"}},
    "fields": {"step": {"source": "facts", "column": "step", "cast": "string"}},
    "parameters": {"period_type": {"type": "string", "default": "month"}}
  }
}
```

## Wizard and Editor charts

Chart JSON files below `configs/DL objects/charts/<family>/<type>/` declare
`name`, `family`, `type`, and a nonempty `title` for the default widget title.
`key` defaults to the filename stem and `tab` defaults to the chart family.
`id` is optional; `copy_from` records
copy provenance and prevents accidental adoption. Wizard requires `dataset`.
Its `fields` maps SDK slots (`x`, `y`, `y2`, `columns`, `rows`, `measures`,
`points`, `size`, `labels`, `segments`) to ordered field-title lists.

Wizard `title` is the default widget title. Optional `chart_title` overrides the
internal title; explicit null leaves an absent source chart title absent. Pulling
an internal-title edit updates explicitly managed `chart_title` and preserves
`title`. Removing the remote internal title records `chart_title: null` in both the
recipe and pull baseline; missing `chartSettings` also means an absent title.
Definitions without `chart_title` retain the legacy pull behavior of updating
`title` when the remote title exists and keeping it when absent. Wizard pulls replace the complete color/shape encoding settings,
including removed bindings, maps and palettes, while retaining unrelated settings.
Optional Wizard properties are `title`, `show_title` (defaults to true when a
title is declared), `description`, `settings`, `local_fields`,
`aggregated_measures`, `hierarchies`, `layers`, `map_center`, `sort`, `filters`,
`axis_titles`, `labels`, `labels_position`, `formats`, and `totals`.
`sort` contains `[field, direction]` pairs; filters use
`{field, operation, values}`. Local formulas and hierarchies require stable GUIDs.

`settings` maps supported public chart operations to argument objects (or lists
of argument objects). Encoding examples:

```json
{
  "color_by_dimension": {"field": "step"},
  "shape_by_dimension": {"field": "step", "shapes_map": {"visit": "circle"}}
}
```

For Measure Names use `color_by_measure_name: {colors_map: {"Revenue": "#ff0000"}}`
and `shape_by_measure_name: {shapes_map: {"Revenue": "circle"}}`; at least two
measures must be placed. Measure color accepts `field`, optional `mode`,
`palette`, and `reversed`. The selected visualization must support the operation.
Line charts support these Measure Names mappings; scatter builders do not.
A requested setting absent from the selected public builder raises
`DataLensUtilsError` with the chart type and setting name. Imports report
unsupported fidelity and refuse local writing; pulls leave recipes and checkpoints
unchanged. Settings are never silently omitted.

Wizard chart-level `params` cannot be authored through the supported SDK.
Import normalizes source chart defaults into every dashboard placement.
Explicit placement values override those chart defaults. Runtime precedence is
entity default → widget → dashboard global → selector → URL parameters/state.

Editor charts use `scripts`, mapping supported tab names to contained UTF-8
asset paths, and `links`, mapping aliases to dataset semantic keys.
`meta_links_mode` defaults to `authored`: explicit Meta links must agree with
resolved dataset IDs. In `managed` mode only declared aliases are rewritten
inside JSON Meta `links`; all unrelated keys and aliases remain authored.
Copy import selects managed mode automatically. JavaScript Meta cannot be
executed to infer dependencies and blocks import.

## Placements, selectors and dashboard

Version 2 supports repeated references. `configs/UI/widgets.json` maps stable
item IDs to `{chart, tab, title, params, enable_action_params, presentation}`.
`params` defaults to `{}` and `enable_action_params` to false.
`configs/UI/chart_groups.json` maps widget IDs to `{tab, charts, presentation}`.
Each member has its own `key`, a `chart` reference, optional `title`, `default`,
`params`, and action flag. Exactly one member is the default.

```json
{
  "weekly": {"chart": "trend", "tab": "overview", "params": {"period_type": ["week"]}},
  "monthly": {"chart": "trend", "tab": "overview", "params": {"period_type": ["month"]}}
}
```

Layout maps tab keys and item IDs to `[x, y, width, height]`; use integer grid
coordinates. Selector definitions retain source, control, display scope
(`show_on_tabs`) and influence (`affects`), with geometry on each displayed tab.
Wiring uses widget keys, selector keys, or `group/member` routes; a group key
addresses all members. Managed alias groups preserve unrelated user aliases.

`configs/DL objects/dashboard.json` accepts `id`, `copy_from`, `description`,
`hide_tabs`, `folders`, `settings`, `global_parameters`, `support_description`,
and `access_description`. Omitted settings remain unchanged; explicit null resets
supported settings or removes a global parameter.

For the complete deployment sequence and fidelity/recovery boundaries, see
[copying a tab](copy-tab.md) and the [BI workflow](bi-projects.md).

[Module index](index.md)
