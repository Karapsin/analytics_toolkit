[All module docs](../README.md)

# DataLens utilities

## All DataLens Functions

[Public facade reference](functions/index.md)

## Workflow Guides

- [BI projects and dashboard import](bi-projects.md)

Use the separate analytics_toolkit.datalens_utils module for configuration-driven
YC dashboard creation, editing and recovery. See the [module README](../../../analytics_toolkit/datalens_utils/README.md)
and [dashboard skill](../../../.agents/skills/datalens-dashboard/SKILL.md).
Base imports and construction remain dependency-free within the configured core
Python window. DataLens operations require at least Python 3.10 and the `datalens`
extra on a supported interpreter. See the
[Python support policy](../../PYTHON_SUPPORT.md#accepted-compatibility-ranges)
for the explicit core, `datalens`, and `[all]` ranges; `[all]` includes every
optional module. Dashboard projects reuse the already installed
analytics-toolkit[all] environment on an interpreter supported by `[all]`.

[All module docs](../README.md)
