[All module docs](../README.md)

# analytics_toolkit.sql_explorer

`sql_explorer` opens an exploratory terminal interface over a database key from
the current project's `.connections` file. The base package does not install
its interface, syntax, or clipboard dependencies; install the optional extra:

```bash
pip install 'analytics-toolkit[tui]'
```

## All SQL Explorer Functions

- [All SQL explorer functions](functions/index.md)

## Workflow Guides

- [Launch and execution](#launch-and-execution)
- [Tabs and scheduling](#tabs-and-scheduling)
- [Editor and completion](#editor-and-completion)
- [Navigation mode](#navigation-mode)
- [Results and clipboard](#results-and-clipboard)
- [Query status and cancellation](#query-status-and-cancellation)
- [Commands](#commands)

## Launch and execution

Launch from a shell with a `.connections` key:

```bash
analytics-toolkit sql explore gp
```

Omit the key to choose from valid entries inside the terminal, or launch from a
terminal Python or IPython console:

```python
from analytics_toolkit import sql_explorer

sql_explorer.run("gp")
```

The launcher requires an interactive terminal. Notebook kernels and redirected
standard input or output cannot host the TUI.

`Ctrl+Enter` and `F5` are the recommended portable run shortcuts. Explorer Ctrl
shortcuts also accept terminal-forwarded macOS Command and Fn-like modifier
events. `Fn+Enter` runs when reported as keypad Enter, and `Cmd+Enter` runs when
the terminal forwards that chord. Terminals and multiplexers may intercept
Command or Fn before the application receives it, so Ctrl remains portable.

When the editor has non-empty selections, every selected fragment is joined in
document order with newlines, then planned, confirmed when necessary, and
executed. Otherwise, the complete buffer runs. This lets a selected `SELECT`
line and a separately selected `FROM` line execute as one statement. Starting a
query does not replace the editor text or collapse its selections. A selected
multi-statement fragment follows the same routing and mutation-confirmation
rules as a full buffer, and the confirmation dialog shows the SQL that will
execute.

A single row-producing statement uses `sql.read`. A multi-statement selection
or buffer whose final statement produces rows uses `sql.execute_read`. SQL with
no result uses `sql.execute`. Non-read statements require confirmation by
default; exploratory mode is not a database-enforced read-only session.

## Tabs and scheduling

Each tab is an independent SQL workspace with its own editor, command line,
database selection, result or error pane, completion UI, file, and focus
position. Labels use `[db] file.sql`; unsaved work adds `*`, and a new tab uses
`[db] Untitled N`. Click the trailing `+` or press `Ctrl+T` to create a tab.
Click its `×` or press `Ctrl+W` to close it. `Ctrl+Tab` moves forward and
`Ctrl+Shift+Tab` moves backward with wraparound. The final tab remains open.

Closing a changed file or a non-empty untitled tab asks whether to Save, Don't
Save, or Cancel. Closing a running tab first applies that decision, then targets
only that tab's query for cancellation and waits for its worker to finish. A
cancellation failure keeps the tab open. Exiting checks every dirty tab.

Opening a file focuses its existing tab when it is already open. Otherwise it
reuses the active clean untitled tab or opens a new tab, preserving the current
workspace. New tabs inherit the active database, after which `db DB_KEY` changes
only the selected tab.

User queries enter one global FIFO queue. The default concurrency is one across
all tabs, each tab may have only one queued or active query, and the SQL plus
database are captured when Run is pressed. Use `concurrency N` to persist a
different positive limit; values above three require overload confirmation.
Lowering the limit does not cancel work already running. Metadata completion
uses separate FIFO queues: tabs on the same database alias share one queue and
cache, while different aliases have independent queues that may run in parallel
with user SQL.

## Editor and completion

After executing a command, focus remains in the command pane. Secondary editor cursors remain visible on empty lines as well as lines containing text.

The editor and text inputs use steady non-blinking carets. The editor displays
line numbers, applies SQL syntax highlighting, and keeps long SQL lines
unwrapped. The `tui` extra installs the Textual 0.73 legacy parser
stack on Python 3.8–3.12 and Textual 0.89 with `tree-sitter-sql` on Python
3.13–3.14, so highlighting is available on every supported Python version. If
a parser installation is damaged or cannot load, the editor still opens
without highlighting and remains editable.

Portable editing keys include Home, End, arrows, Shift+arrows, Ctrl+A,
Ctrl+Left, Ctrl+Right, Tab, Shift+Tab, Enter, Escape, Ctrl+C, Ctrl+Enter, and F5.
Home always moves every cursor to column zero of its current logical line and
collapses its selection; Ctrl+Home goes to the start of the document. Ctrl+A
selects the full buffer. Ctrl+Left and Ctrl+Right move every cursor by one word,
and their Shift variants extend every cursor's independent selection. Shift+Up
and Shift+Down add a cursor above or below the active cursor. Repeating the key
chains cursors across as many logical lines as needed;
pressing toward an occupied adjacent line removes that cursor, while at least one
cursor always remains. Each cursor has an independent selection, and editing or
pasting applies at every cursor. Escape collapses multiple cursors to the active
one before toggling focus between the editor and command panel. Tab and Shift+Tab indent or
unindent selected logical lines. Double-clicking selects a complete SQL word such
as `table_name`.

`Ctrl+F` opens a compact Find/Replace overlay on the right side of the query
area. Find, Replace, Replace All, match highlighting, and Escape-to-close remain
available without reducing the normal editor width. While it is open, Up and
Down cycle through its controls; Left and Right keep their normal text-caret
behavior in its inputs.

Tab is conditional:

1. If the completion menu is open, Tab accepts its highlighted suggestion.
2. If the cursor has one matching suggestion, Tab inserts it immediately.
3. If the cursor has multiple matches, Tab opens the suggestion menu.
4. If metadata is needed, Tab requests it and applies the same one-or-many rule.
5. Otherwise, Tab performs normal indentation.

The editor keeps keyboard focus while the menu is open, so continued typing or
backspacing filters the visible options. A typed prefix that narrows the menu to
one option remains editable until Tab or Enter accepts it. Up and Down move the
highlight, and Escape closes the menu. Local keyword completion uses lower-case
SQL keywords and does not query a database. Backend metadata completion supports
Trino catalogs, schemas, and tables; Greenplum schemas and tables; and ClickHouse
databases and tables. Trino catalog discovery starts when the Explorer opens,
then schema discovery is queued for each catalog. Same-request lookups from
multiple tabs fan out from one metadata operation. Closing a tab removes its
queued callbacks without affecting other subscribers. Metadata never replaces
the visible user-query status.

Table completion is available only after `FROM`, `JOIN`, `UPDATE`, or `INTO`.
The prefix must contain at least six characters. The first Tab request calls
`sql.show_tables(...)` once with that initial six-character prefix and current
catalog/database/schema context. Results are cached; longer prefixes and
backspacing filter those candidates locally without another metadata query. A
catalog, database, schema, or SQL-clause context change permits a new lookup.

## Navigation mode

File navigation is a separate modal mode, not a permanent workspace pane. Open
an existing file with `Ctrl+O`, terminal-forwarded `Cmd+O`, `open`, or `mode
navigation`. Escape returns to the editor without opening a file. `Ctrl+S` or
`save` writes edits back to the opened `.sql` file. When the tab is untitled,
Save starts the new-file flow, writes the editor text exactly, and continues in
that file without replacing editor state. `Ctrl+N` always creates a blank SQL
file; it reuses a clean untitled tab or opens the file in a new tab so an active
file or dirty buffer stays untouched. Both flows ask for a filename and
destination directory and never overwrite an existing file.

Navigation starts at resolved `Path.cwd()` on the host running the Explorer.
Consequently, an Explorer launched over SSH reads the remote host's filesystem,
not the Linux client's filesystem. Directories appear first, then all regular
files, with case-insensitive sorting; hidden and non-SQL files remain visible.
Symlinks that resolve outside the navigation root are omitted.

Normal typing goes into the path field and filters the current path segment.
Tab completes a sole match or cycles through multiple visible candidates;
Shift+Tab, Up, and Down move through those candidates. Enter or click descends
into a directory. On a file, Enter or click opens it only when its suffix is
`.sql` (case-insensitive); other files remain visible but cannot be opened.
The current SQL file path appears in status. File browsing never renames or
deletes files; save writes only an opened existing `.sql` file, and new-file
creation is limited to the selected project directory. File, decoding, and
permission failures appear in the originating tab's result/message surface and
do not terminate the Explorer.

## Results and clipboard

At most 200 result rows are displayed. Query-shaped final statements use a
201-row server-side limit so the Explorer can indicate when more rows exist
without fetching an unbounded result. Finite Decimal cells display without
insignificant trailing zeros. Integers, decimals, and floating-point values use
comma thousands separators. Nulls display as `NULL`, and embedded tabs or line
breaks are escaped visibly.

Rows have visual labels beginning at 1. Click selects one data cell; drag or
Shift-click creates an inclusive rectangular range; Shift+arrows extend it.
Plain arrows clear the rectangle and move the active cell. Up from the first
result row selects its column header; Left and Right move between headers, Down
returns to the same column's first row, and a second Up leaves the result pane.
Clicking a header selects only that header label. Ctrl+C serializes selected displayed data as
tab-separated columns and newline-separated rows. Visual row labels are never
copied, and an ordinary data rectangle never gains a header row.

Copy first emits a base64-encoded OSC 52 sequence to the active terminal. This
is the SSH-compatible route by which a terminal on a Linux client can place
remote Explorer text in the client's local clipboard. OSC 52 may be disabled by
terminal or multiplexer policy. The Explorer still attempts remote Pyperclip
and keeps an in-memory fallback, but it does not assume that a remote macOS
clipboard and a local Linux clipboard are the same. Bracketed paste is the
portable SSH paste path; Ctrl+V/Pyperclip remains a local fallback.

## Query status and cancellation

The status area retains the latest user-query label, SQL route, state, and
elapsed duration after success, failure, cancellation, result clearing, pane
closing, or focus changes. Active elapsed time refreshes about once per second.
After five minutes, status adds a bold red warning:
`consider optimizing your query or sit tight`.

The `Interrupt` button and `cancel` command use the same targeted cancellation
path. They identify only the active Explorer user-query label through
`sql.show_queries(...)` and call `sql.cancel_queries(...)` for matching IDs.
Metadata completion queries are independent and are never interruption targets.
The button remains disabled while no user SQL runs or while cancellation is
pending; the interface remains busy until the SQL worker acknowledges completion
or cancellation.

Escape toggles between the editor and command panel. From a visible result/error
pane, Escape moves to the command panel; use Up and Down at pane boundaries to
enter or leave results/errors. A confirmation or navigation modal consumes Escape
first; Find/Replace and completion overlays close before focus navigation.

## Commands

Enter commands in the lower panel, with or without a leading colon:

- `run` - run the selection or complete editor buffer
- `open` - enter remote-host SQL file navigation mode
- `save` - save edits to the opened SQL file
- `cancel` - request cancellation of the active Explorer user query
- `mode [exploratory|navigation]` - show the current mode or enter navigation
- `mv LINE_NUMBER` - move to the start of a one-based editor line
- `mvs LINE_NUMBER` - select from the active cursor to a one-based line start
- `cp` - copy selections in document order, or the whole editor when unselected
- `pst` - paste the Explorer clipboard once at every cursor or selection
- `db DB_KEY` - switch to another valid configured connection
- `shortcut KEY|reset` - save a run shortcut or restore `Ctrl+Enter`
- `confirm on|off|toggle` - change and save mutation confirmation
- `concurrency N` - set and save the global user-query concurrency limit
- `clear query|results|all` - clear workspace content
- `to_excel` - choose a project directory and save the current result as `.xlsx`
- `to_csv` - choose a project directory and save the current result as `.csv`
- `help` - open the in-app command reference
- `exit` or `quit` - close the Explorer, requesting targeted cancellation first
  when user SQL is running

The confirmation choice, query concurrency, and primary run shortcut are saved
in the user's config directory. SQL text is not persisted. The export commands first ask for one
filename with the required suffix, then reuse the project directory browser. An
existing destination requires replacement confirmation. If a result has more than
200 rows, confirm saving all rows before the Explorer reruns a capped query.

In every two-choice confirmation dialog, Left selects the affirmative action and
Right selects cancellation; Enter activates the focused choice. The existing Y,
N, Escape, and mouse controls remain available.

[All module docs](../README.md)
