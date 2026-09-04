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
- `Ctrl+Enter` and `F5` are the portable run shortcuts. Explorer Ctrl shortcuts
  accept terminal-forwarded Command/Fn-like events when available.
- `Ctrl+T` creates a complete workspace tab, `Ctrl+W` closes it safely, and
  `Ctrl+Tab` / `Ctrl+Shift+Tab` switch with wraparound.
- User SQL uses a shared FIFO queue per database, with at most one active user
  query on each database. Metadata uses a separate shared FIFO queue for each
  database alias.
- `Ctrl+O`, `Cmd+O` when forwarded, `open`, or `mode navigation` opens a
  read-only browser rooted at the running process's current directory. Its path
  input supports Tab completion; every in-root file is visible, but only `.sql`
  files can be opened. Over SSH, that directory belongs to the remote host.
- `Ctrl+S` creates a file for an untitled buffer and saves its exact text;
  `Ctrl+N` creates a blank file without replacing a dirty or opened tab.
- Copy emits OSC 52 before trying Pyperclip, allowing a supporting SSH client
  terminal to place text in its local clipboard.
- `Tab` inserts a sole completion directly. With multiple matches, the menu
  remains open while typing or backspacing filters its lower-case SQL options.
- Result integers, decimals, and floating-point values use comma thousands
  separators, including in copied visible-value TSV text.
- The function returns after the user exits the TUI.

[Functions index](index.md)
