[All module docs](../README.md)

# analytics_toolkit.sql_explorer

`sql_explorer` opens an exploratory terminal interface over a database key from
the current project's `.connections` file. Its optional interface dependencies
are not installed with the base package:

```bash
pip install 'analytics-toolkit[tui]'
```

Launch it from a shell:

```bash
analytics-toolkit sql explore gp
```

Omit the key to choose from valid `.connections` entries inside the terminal:

```bash
analytics-toolkit sql explore
```

Or from a terminal Python or IPython console:

```python
from analytics_toolkit import sql_explorer

sql_explorer.run("gp")
```

Calling `sql_explorer.run()` without an argument opens the same terminal picker.

The launcher requires an interactive terminal. Notebook kernels and redirected
standard input or output are rejected because they cannot host the TUI.

## Workspaces and keys

The main workspace starts as a SQL editor. A successful row-producing query or
an error splits it evenly with a result pane. A short command panel remains at
the bottom.

- `Ctrl+Enter` runs the complete editor buffer by default; `F5` always runs it.
  `Fn+Enter` also runs when the terminal reports it as keypad Enter.
  `Cmd+Enter` runs when a macOS terminal forwards the Command modifier. Some
  terminal profiles or operating-system shortcuts intercept these keys, so
  `Ctrl+Enter` and `F5` remain the portable choices.
- `Alt+Tab` and `Alt+Shift+Tab` cycle the editor, visible result pane, and
  command input. macOS terminals report the Option key as Alt.
- Up and Down cross to the preceding or following pane at the first or last
  editor line and first or last result row. They always cross from the command
  input. Inside those boundaries they retain normal cursor movement.
- Plain `Tab` and `Shift+Tab` indent and unindent in the SQL editor.
- The editor displays line numbers. `Home` and `End` always move to the current
  line's absolute start and end; adding Shift selects to that edge.
- `Ctrl+F` opens a VS Code-style find/replace bar with Find and Replace inputs
  plus Next, Replace, and Replace All buttons. Matches are case-insensitive,
  wrap around, and are all highlighted in bright yellow. Enter advances from
  Find or replaces the current match from Replace; Escape closes the bar.
- `Delete` closes a focused result or error pane and expands the editor.
- Editing is non-modal and includes the usual select-all, cut, copy, paste,
  undo, redo, document-start, and document-end shortcuts.

At most 200 rows are displayed. Query-shaped final statements are wrapped with
a 201-row server-side limit so the explorer can indicate when further rows are
available without fetching an unbounded result. The wrapper preserves the
query's line layout so backend error line numbers correspond to editor numbers.
Finite Decimal cells are displayed without insignificant trailing zeros, while
their exact dataframe values remain unchanged.

## Execution and safety

A single row-producing statement uses `sql.read`. A multi-statement buffer whose
last statement produces rows uses `sql.execute_read`. Buffers without a result
use `sql.execute`. Non-read statements require confirmation by default; this is
an exploratory mode, not a database-enforced read-only session.

The confirmation choice and primary run shortcut are saved in the user's config
directory. SQL text and query results are not persisted by the explorer.

## Commands

Enter commands in the lower panel, with or without a leading colon:

- `run` runs the editor buffer.
- `cancel` requests cancellation of the active query started by this explorer.
- `mode [exploratory]` displays or selects the only current mode.
- `db DB_KEY` switches to another valid configured connection.
- `shortcut KEY` saves the primary run shortcut; `shortcut reset` restores
  `Ctrl+Enter`.
- `confirm on|off|toggle` changes and saves mutation confirmation.
- `clear query|results|all` clears workspace content.
- `help` opens the in-app command reference.
- `exit` or `quit` closes the explorer. If a query is running, the explorer
  requests targeted cancellation first and exits after that request completes.

## All SQL Explorer Functions

- [All SQL explorer functions](functions/index.md)

## Workflow Guides

Workspace behavior, execution routing, safety, and commands are described in
the sections above.

[All module docs](../README.md)
