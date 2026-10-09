# Advanced DataLens BI example

Run `python dashboard.py validate` in the installed toolkit environment. This
portable example uses synthetic IDs and `example.sales`/`example.calendar`;
provide your deployment, existing connection and source schema before applying.
No environments or tools are installed by the project.

```sh
python dashboard.py capabilities --json
python dashboard.py plan --resource chart:wizard_revenue --json
python dashboard.py import-dashboard --dashboard-id SOURCE --json
python dashboard.py import-tab --dashboard-id SOURCE --tab-id TAB --json
```

Import previews a merge into this recipe. Resolve fidelity blockers and collisions
before adding `--write`. Import changes no cloud resources. Verification compares
managed metadata and revisions; numerical SQL scenarios remain independent.

The recipe demonstrates an explicit join, source-qualified identities, RLS,
cache configuration, dataset parameters, chart variants, member wiring and HTML.
Change folders to workbook targets through a separate deployment identity;
Enterprise uses BIProjectDeployment with installation and base_url.

[Public BI workflow](../../docs/modules/datalens_utils/bi-projects.md)
