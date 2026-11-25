[SQL functions index](index.md)

# validate_connections

Validate `.connections` entries, optionally probing live backend connectivity.

```python
validate_connections(keys: 'Sequence[str] | None' = None, *, connect: 'bool' = False) -> 'list[ConnectionValidationResult]'
```

## Inputs

- `keys` - connection keys to validate; `None` validates all configured keys
- `connect` - whether validation should open each backend and execute and consume one `SELECT 1`

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

With `connect=False`, validation performs no network access. `connect=True`
uses a validation-only five-second connection/read budget (or a tighter configured
limit), disables retries, and closes connections on success and failure. Lazy
clients must complete the query before `connected=True` is reported. Driver
errors are sanitized; credentials and server-provided response text are omitted.

[SQL functions index](index.md)
