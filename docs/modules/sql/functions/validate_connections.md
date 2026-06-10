[SQL functions index](index.md)

# validate_connections

Validate `.connections` entries, optionally opening live backend connections.

```python
validate_connections(keys: 'Sequence[str] | None' = None, *, connect: 'bool' = False) -> 'list[ConnectionValidationResult]'
```

## Inputs

- `keys` - connection keys to validate; `None` validates all configured keys
- `connect` - whether validation should open each selected backend connection

## Usage

```python
from analytics_toolkit import sql

for result in sql.validate_connections(["gp", "trino"]):
    print(result.connection_key, result.valid, result.error)
```

Output example:

```text
gp True None
trino True None
```

[SQL functions index](index.md)
