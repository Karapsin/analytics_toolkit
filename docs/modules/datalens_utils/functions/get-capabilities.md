[Functions index](index.md)

# get_capabilities

Report individual public SDK operations, adapter restrictions and prerequisites
without authentication, network calls, recipe reads or runtime writes.

`get_capabilities(*, installation="yc")`

## Inputs

- `installation` - `yc` or `enterprise`

## Usage

```python
from analytics_toolkit.datalens_utils import get_capabilities

report = get_capabilities(installation="enterprise")
print(report["operations"]["html_page.pull"]["status"])
# blocked
```

Reports include matrix version, installed SDK version, installation, authoritative
connector/source/chart inventories, and per-operation `status`, `sdk_status`,
`adapter_status`, reasons, restrictions and requirements. Missing or untested
SDK versions are unavailable. `project.capabilities()` uses its deployment.

[Functions index](index.md)
