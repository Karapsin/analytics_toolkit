[Functions index](index.md)

# Errors

`DataLensUtilsError(message)` is the library failure base.
`DataLensConfigurationError(message)` identifies invalid recipe/deployment input.
`DataLensDependencyError(message)` identifies unsupported Python or a missing or
incompatible optional SDK. Public SDK exceptions retain their types and request IDs.

## Usage

```python
from analytics_toolkit.datalens_utils import DataLensConfigurationError, ProjectPaths
from pathlib import Path

try:
    ProjectPaths(Path("/recipe"), Path("/recipe/runtime"))
except DataLensConfigurationError as error:
    print(error)
# The runtime directory must be outside the shareable project.
```

[Functions index](index.md)
