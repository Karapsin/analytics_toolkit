[Functions index](index.md)

# run

Open the exploratory SQL terminal interface for a configured connection.

```python
run(db_key: 'str | None' = None) -> 'None'
```

## Inputs

- `db_key` - optional connection key or alias from the project `.connections`
  file; when omitted, a terminal connection picker opens first

## Usage

```python
from analytics_toolkit import sql_explorer

sql_explorer.run("gp")
```

Open the connection picker instead:

```python
sql_explorer.run()
```

## Notes

- Install `analytics-toolkit[tui]` before launching the interface.
- Run this function from an interactive terminal Python or IPython console, not
  a notebook.
- The function returns after the user exits the TUI.

[Functions index](index.md)
