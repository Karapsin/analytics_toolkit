[SQL functions index](index.md)

# set_missing_env_variables

Prompt securely for environment variables referenced by `.connections` that
are unset or empty, then set them in the current Python process.

```python
set_missing_env_variables() -> 'list[str]'
```

## Inputs

This function has no inputs.

## Usage

```python
from analytics_toolkit import sql

names = sql.set_missing_env_variables()
# Enter value for environment variable 'GP_PASSWORD':
# Enter value for environment variable 'S3_ACCESS_KEY':
```

Output example:

```python
names
# ['GP_PASSWORD', 'S3_ACCESS_KEY']
```

## Notes

- Prompts use `getpass`, so entered values are not echoed.
- Each environment variable is prompted once even when several connection fields reference it. Existing non-empty values are left unchanged.
- Empty responses are prompted again. Collected values are applied together after every prompt succeeds, so cancelling does not leave a partially updated environment.
- Values are set only in the current Python process and its future child processes; a Python process cannot update its parent shell environment.
- The helper reads direct connections and Airflow-source overrides from `.connections`, but does not contact Airflow or a database. It ignores `airflow_variable` references.

[SQL functions index](index.md)
