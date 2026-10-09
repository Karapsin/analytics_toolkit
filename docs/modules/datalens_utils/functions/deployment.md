[Functions index](index.md)

# Deployment and ProjectPaths

`Deployment(organization_id, target_path, dashboard_name, connection_name, connection_id, yc_profile, yc_binary="yc")`

This immutable identity contains ordinary resource IDs and an existing CLI/profile.
It contains no credentials. `as_dict()` returns its string-valued fields.

`ProjectPaths(project_root, runtime_root)` resolves immutable Path fields and
rejects runtime locations inside the recipe, including symlink aliases.

## Usage

```python
from pathlib import Path
from analytics_toolkit.datalens_utils import ProjectPaths

paths = ProjectPaths(Path("/project/recipe"), Path("/project/runtime"))
# ProjectPaths(project_root=PosixPath('/project/recipe'), runtime_root=PosixPath('/project/runtime'))
```

## BI deployment identities

`TargetLocation.path(path)` or `TargetLocation.workbook(*, by_id=None, key=None)`
selects a folder or workbook. Exactly one workbook identifier is required.

`BIProjectDeployment(dashboard_name, target, *, installation="yc", organization_id=None, yc_profile=None, yc_binary="yc", base_url=None, token_env=None)`
supports YC and Enterprise. Enterprise requires base_url; token_env references
an external token. These types remain SDK-free and do not read files.

```python
from analytics_toolkit.datalens_utils import BIProjectDeployment, TargetLocation

identity = BIProjectDeployment("Sales", TargetLocation.workbook(by_id="workbook"),
                               installation="enterprise", base_url="https://bi.example.test")
print(identity.as_dict()["installation"])
# enterprise
```

[Functions index](index.md)
