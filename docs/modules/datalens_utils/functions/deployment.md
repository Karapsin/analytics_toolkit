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

[Functions index](index.md)
