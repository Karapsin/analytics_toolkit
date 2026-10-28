[Functions index](index.md)

# import_dashboard and DataLensAuthConfig

Capture a published dashboard into a new standard project without cloud writes.

```python
DataLensAuthConfig(installation="yc", organization_id=None, yc_profile=None,
                   yc_binary="yc", base_url=None, token_env=None)

import_dashboard(source, destination, *, auth, runtime_root=None, tab_ids=None,
                 dry_run=True, resume=False, client_factory=None, reporter=None)
```

## Inputs

- `source` - dashboard URL or ID for the configured installation
- `destination` - absent or empty local project directory
- `auth` - DataLensAuthConfig containing existing authentication references
- `runtime_root` - external private state directory; defaults to DATALENS_RUNTIME_DIR or a sibling .local directory
- `tab_ids` - optional sequence of tab IDs; None includes all visible and hidden tabs
- `dry_run` - preview when True; False creates the local project
- `resume` - reuse exact-revision cached captures after checking current inventories
- `client_factory` - optional injected public SDK client context factory for offline tests
- `reporter` - optional progress callback accepting print-style arguments
- `installation` - yc or enterprise
- `organization_id` - existing YC organization identifier
- `yc_profile` - existing YC CLI profile name
- `yc_binary` - existing YC CLI executable
- `base_url` - existing Enterprise API base URL
- `token_env` - environment variable name holding an existing token, never the token value

## Usage

```python
from analytics_toolkit.datalens_utils import DataLensAuthConfig, import_dashboard

report = import_dashboard(
    "https://datalens.ru/dashboard-id", "/projects/imported",
    auth=DataLensAuthConfig(organization_id="org", yc_profile="existing"),
    runtime_root="/private/imported-runtime",
)
# {"operation": "import-dashboard", "written": False,
#  "capture_complete": True, "revision_inventory_stable": True,
#  "coverage": {...}, "resources": {...}, "request_count": ...,
#  "cache_hits": ..., "capture_issues": [], ...}
```

The report separates preserved source from editing support per resource. SQL and
Editor tabs are not executed. Missing permissions are reported without bypassing
access controls. Exceptions retain their typed SDK context; failure details and
completed captures remain under the external runtime. See the
[workflow](../import-dashboard.md) for CLI, recovery and verification.

[Functions index](index.md)
