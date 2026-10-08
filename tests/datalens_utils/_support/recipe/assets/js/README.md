# Editor assets

Each persistent Editor object has its own JSON definition under `configs/DL objects/charts/editor/<renderer>/` (Gravity uses an additional `<series-or-variant>/` directory) or `configs/DL objects/selectors/editor/selector/`. Definitions map complete public Editor tabs to these JavaScript files. The Python adapter generates Meta JSON from the definition's `links` aliases and current dataset IDs; credentials never belong in these assets.

Start production adaptation by editing the JSON definitions, source field lists, parameter bindings, and Prepare transformations. Reuse the Python adapter. Every visual uses a linked dataset, `Editor.getParams()`, `Dataset.buildSource(...)`, and `Dataset.getDatasetRows(...)`; series are built from actual query results. The fixture file is used only by offline checks.

Common defaults are arrays of strings. `region` supports multiple values, `channel` supports one value, and `All` means no dimension filter. Interval filters use `interval_from` and `interval_to`; snapshot cards consume only `snapshot_date`. Extra table filters consume `min_revenue`, `search` (case-insensitive category matching), and `only_priority`. Markdown consumes `annotation`, escaping markup characters before display. The selector provides all eight documented control types plus a preset and reset button.

Source queries include Row ID to preserve the fact grain while selecting aggregated dataset measures. They request at most 10,000 rows, which exceeds these toy dataset sizes. A production source must aggregate appropriately, paginate, or raise that explicit limit after assessing volume; silently truncating data changes metrics. Acquisition funnel counts are aggregated within each stage. Profit margin and average order value divide totals rather than averaging row ratios. X-range shows complete tasks from the latest selected date.

Run the deterministic offline checks with:

```sh
node assets/js/verify.mjs
```

The checks exercise the script protocol, source filters, formulas, all series/control coverage, empty selections, numeric validation, presets, isolated snapshot dates, and Advanced rendering without closures. They do not establish rendering or dashboard interaction in DataLens. This project's verification is API and code only; browser behavior remains unverified by user scope.

The example retains the 14 Gravity series types investigated for attempt 4 and a separate donut variant (15 Gravity charts), plus Advanced, Markdown, and table. Sankey has its own upstream public series API. Persisted configuration does not prove that the deployed DataLens renderer supports that upstream version. Keep this rendering gap explicit and preserve the declared series.

Sankey constructs ordered acquisition progression and stage-specific drop-off links, preserving each stage count. This assumes a single cohort with nonincreasing counts across stages. A production source must establish that cohort relationship. The Markdown chart also displays **Toy Compound Score**, `SUM(Revenue) + SUM(Profit) * (SUM(Orders))`, solely to demonstrate operator precedence and aggregated expressions; it is explicitly not a business metric.

Authoritative runtime references:

- [Editor tabs, aliases, defaults, and sources](https://yandex.cloud/en/docs/datalens/charts/editor/tabs)
- [Dataset query conditions and parameterized sources](https://yandex.cloud/en/docs/datalens/tutorials/create-chart-editor)
- [Dataset rows and table transformations](https://yandex.cloud/en/docs/datalens/charts/editor/quickstart/from-dataset)
- [Gravity UI renderer](https://yandex.cloud/en/docs/datalens/charts/editor/widgets/gravity-ui) and [series inventory](https://gravity-ui.github.io/charts/pages/overview.html)
- [Advanced renderer and browser sandbox](https://yandex.cloud/en/docs/datalens/charts/editor/widgets/advanced)
- [Markdown renderer](https://yandex.cloud/en/docs/datalens/charts/editor/widgets/markdown)
- [Table renderer](https://yandex.cloud/en/docs/datalens/charts/editor/widgets/table)
- [All eight control types and button actions](https://yandex.cloud/en/docs/datalens/charts/editor/widgets/controls)
- [Current upstream Sankey series API](https://github.com/gravity-ui/charts/blob/main/src/core/types/chart/sankey.ts)
- [Dataset v2 service module implementation](https://github.com/datalens-tech/datalens-ui/blob/main/src/server/modes/charts/plugins/dataset/v2.ts)
