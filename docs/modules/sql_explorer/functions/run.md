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
- Editor and text-input carets remain visible and do not blink.
- `Ctrl+Enter` and `F5` are the portable run shortcuts; terminal-forwarded
  `Cmd+Enter` is optional.
- `Ctrl+O`, `Cmd+O` when forwarded, `open`, or `mode navigation` opens a
  read-only browser rooted at the running process's current directory. Its path
  input supports Tab completion; every in-root file is visible, but only `.sql`
  files can be opened. Over SSH, that directory belongs to the remote host.
- Copy emits OSC 52 before trying Pyperclip, allowing a supporting SSH client
  terminal to place text in its local clipboard.
- `Tab` inserts a sole completion directly. With multiple matches, the menu
  remains open while typing or backspacing filters its lower-case SQL options.
- Result integers, decimals, and floating-point values use comma thousands
  separators, including in copied visible-value TSV text.
- The function returns after the user exits the TUI.

[Functions index](index.md)
