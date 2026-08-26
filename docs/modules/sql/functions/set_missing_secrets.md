[SQL functions index](index.md)

# set_missing_secrets

Prompt securely for absent or empty `.secrets` values referenced by
`.connections`, then persist them in the secret file beside `.connections`.

```python
set_missing_secrets() -> 'list[str]'
```

## Inputs

This function has no inputs.

## Usage

In `.connections`, reference persistent secrets by name:

```json
{
  "gp": {
    "type": "gp",
    "host": "gp.example",
    "user": "analytics_user",
    "password": {"from": ".secrets", "key": "GP_PASSWORD"},
    "database": "analytics"
  }
}
```

Then populate missing values interactively:

```python
from analytics_toolkit import sql

names = sql.set_missing_secrets()
# Enter value for secret 'GP_PASSWORD':
```

Output example:

```python
names
# ['GP_PASSWORD']
```

The resulting `.secrets` entry is zsh-sourceable:

```zsh
export GP_PASSWORD='entered-value'
```

## Notes

- Prompts use `getpass`, so entered values are not echoed.
- Each secret is prompted once even when several connection fields reference it. Existing non-empty values and unreferenced entries remain unchanged.
- Set an existing entry to `''` when it should be prompted again. Empty prompt responses are retried.
- All prompts complete before an atomic file replacement. Cancellation and concurrent file edits do not leave partially collected values behind.
- New files use mode `0600` on POSIX. Existing files with group or other permissions emit a warning, and updates preserve their current mode.
- The helper reads direct and Airflow-source `.connections` metadata without contacting Airflow or a database.
- Add `.secrets` to the consuming project's `.gitignore` and never commit it.

[SQL functions index](index.md)
