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
- [Creating tables](#creating-tables)

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
`Ctrl+Shift+Tab` moves backward with wraparound, like browser tabs.
`Ctrl+PageDown` and `Ctrl+PageUp` remain available as alternatives. The final tab remains open.

Closing a changed file or a non-empty untitled tab asks whether to Save, Don't
Save, or Cancel. Closing a running tab first applies that decision, then targets
only that tab's query for cancellation and waits for its worker to finish. A
cancellation failure keeps the tab open. Exiting checks every dirty tab.

Opening a file focuses its existing tab when it is already open. Otherwise it
reuses the active clean untitled tab or opens a new tab, preserving the current
workspace. New tabs inherit the active database, after which `db DB_KEY` changes
only the selected tab.

Changing databases preserves the tab's filename or `Untitled N` label and dirty
marker, resizing the tab to fit the updated database alias.

User queries enter a shared FIFO queue for their selected database. At most one
user query runs on each database across all tabs, while queries for different
databases may run in parallel. Each tab may have only one queued or active
query, and the SQL plus database are captured when Run is pressed. Metadata
completion uses separate FIFO queues: tabs on the same database alias share one
queue and cache, while different aliases have independent queues that may run
in parallel with user SQL.

## Editor and completion

The `format` command formats selected SQL, or the complete active editor when
nothing is selected, through
[`sql_format.format_sql`](../sql_format/functions/format_sql.md). It uses the
active database dialect and the formatter's defaults, including lowercase
keywords, four-space indentation, `where 1=1` normalization, and positional
grouping/ordering where applicable. Multiple statements are formatted separately
with a blank line between them. Formatting is local, does not execute SQL, and
can be undone in one step. If any selected statement cannot be formatted, no
editor text changes. Command focus stays in the command pane.

Selectable controls share the startup database picker's subtle mouse-hover
highlight. Keyboard selections use the active tab's amber background and dark
text across buttons, tabs, database and completion lists, file navigation,
and result cells. Editor and input text selections retain their previous
translucent highlighting. Hovering a selected control keeps its amber selection
visible. Unselected dialog buttons use neutral colors.

Clicking anywhere in the command pane focuses command entry. Clicking the
editor pane border or line/column status focuses the editor without moving its
cursor or selection. Buttons, search fields, and completion choices still
perform their actions. After executing a command,
focus remains in the command pane. Secondary editor cursors remain visible on empty lines as well as lines containing text.

The editor and text inputs use steady non-blinking carets. The compact active
tab uses a dark surface with amber text and always includes its database key.
The editor displays line numbers, keeps its vertical scrollbar on the right,
shows the active one-based line and column in its lower-right corner without
covering the first editor rows, applies SQL syntax
highlighting, and keeps long SQL lines unwrapped. The `tui` extra installs the Textual 0.73 legacy parser
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

`Ctrl+F` opens a floating Find/Replace overlay on the upper-right side of the
query area, using 40% of its width (32–56 columns) and 8 terminal rows.
Single-row fields and buttons leave one blank row between inputs and actions.
A right-aligned `×` close button sits on its own row above the Find input.
Below it, Find, Replace, Next, Replace, and Replace All appear in that order without reducing the normal editor width. Match highlighting and
Escape-to-close remain available. The close button also clears search
highlights and returns focus to the editor. Search feedback appears in the
command pane only while Find/Replace is open; closing it clears that feedback
without removing a newer command or query notice. While it is open, Up and Down cycle through
its controls; Left and Right keep their normal text-caret behavior in its
inputs. When Replace or Replace All is focused, either Left or Right switches
to the other button without executing it; Enter activates the focused button.

Every single-line field, including commands, Find/Replace, navigation paths,
and export or SQL filenames, supports Shift selection, word selection,
`Ctrl+A`, copy, cut, paste, undo, and redo. Terminal-forwarded Command keys use
the same portable Ctrl behavior. Typing, pasting, or deleting replaces the
active selection, and pasted values remain single-line.

Tab is reserved for completion and indentation. Shift+Tab requests columns in
a SELECT projection even with a blank prefix; elsewhere it unindents. Neither
key moves focus. Ctrl+Space also requests completion without indentation.
With multiple editor cursors, completion is disabled and Tab only indents.

In the editor, Tab is conditional:

1. If the completion menu is open, Tab accepts its highlighted suggestion.
2. If the cursor has one matching suggestion, Tab inserts it immediately.
3. If the cursor has multiple matches, Tab opens the suggestion menu.
4. If metadata is needed, Tab requests it and applies the same one-or-many rule.
5. Otherwise, Tab performs normal indentation.

Completion menus shrink to their remaining matching rows. Catalog, schema,
table, keyword, and column suggestions all filter locally as the prefix changes.

The editor keeps keyboard focus while the menu is open, so continued typing or
backspacing filters the visible options. A typed prefix that narrows the menu to
one option remains editable until Tab or Enter accepts it. Up and Down move the
highlight, and Escape closes the menu. Local keyword completion uses lower-case
SQL keywords and does not query a database. Backend metadata completion supports
Trino catalogs, schemas, and tables; Greenplum schemas and tables; and ClickHouse
databases and tables. Trino catalog discovery starts when the Explorer opens,
then schema discovery is queued for each catalog. Same-request lookups from
multiple tabs fan out from one metadata operation. Closing a tab removes its
queued callbacks without affecting other subscribers. Every cursor-position or
text change cancels that tab’s pending metadata request; the server query is
cancelled when no other subscriber still needs it. Dismissed requests cannot
reopen the menu, including after the cursor returns to its old position. Metadata never replaces
the visible user-query status.

Table completion is available only after `FROM`, `JOIN`, `UPDATE`, or `INTO`.
The prefix must contain at least six characters. The first Tab request calls
`sql.show_tables(...)` once with that initial six-character prefix and current
catalog/database/schema context. Results are cached; longer prefixes and
backspacing filter those candidates locally without another metadata query. A
catalog, database, schema, or SQL-clause context change permits a new lookup.

Column completion is available in SELECT projections when the source tables are
present after FROM/JOIN. A non-keyword identifier prefix must touch the cursor
on its left on the same line, and the character cell immediately on its right
must be whitespace or the end of the line. Blank prefixes, keywords, and positions
inside words do not request columns with Tab. Shift+Tab explicitly allows a blank
left-hand prefix, while keeping the right-hand whitespace and single-cursor rules.
It resolves aliases, joined tables, nested SELECTs,
derived tables, and CTE output names, including explicit column lists and stars.
Type a prefix after `alias.` to restrict suggestions to that source; ambiguous unqualified
columns are offered with their source qualifier. Tab requests columns through
[`sql.table_info`](../sql/functions/table_info.md) without counting rows or
executing the editor SQL. Results share the database metadata queue/cache.
Unresolvable SQL and recursive outputs without determinable names are omitted.

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
Up and Down move through those candidates. Enter or click descends
into a directory. On a file, Enter or click opens it only when its suffix is
`.sql` (case-insensitive); other files remain visible but cannot be opened.
The current SQL file path appears in the tab tooltip. File browsing never
renames or deletes files; save writes only an opened existing `.sql` file, and
new-file creation is limited to the selected project directory. File, decoding, and
permission failures appear in the originating tab's result/message surface and
do not terminate the Explorer.

When navigation is choosing a destination directory, the first Escape focuses
and arms **Select this directory**. Enter then confirms the displayed directory;
a second Escape cancels. Any arrow key returns to path navigation and also
performs its normal move. The ordinary open-file browser still closes on its
first Escape.

## Results and clipboard

Click the top-right `×` in the results/error pane to dismiss output and return
to the editor. Closing output preserves SQL text and query outcome/timing.

At most 200 result rows are displayed, with the result grid's vertical
scrollbar on the right. The query, result, and command panes use square borders
with a one-row separation so their focus treatments never overlap.
Query-shaped final statements use a
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
Clicking a header selects only that header label. Ctrl+C copies underlying cell values as tab-separated columns and newline-separated
rows, without display thousands separators or truncation: an integer shown as
`1,000` copies as `1000`. Text commas and numeric precision are preserved; nulls
remain `NULL`, and embedded tabs or line breaks remain escaped. Visual row labels are never
copied, and an ordinary data rectangle never gains a header row.

Copy first emits a base64-encoded OSC 52 sequence to the active terminal. This
is the SSH-compatible route by which a terminal on a Linux client can place
remote Explorer text in the client's local clipboard. OSC 52 may be disabled by
terminal or multiplexer policy. The Explorer still attempts remote Pyperclip
and keeps an in-memory fallback, but it does not assume that a remote macOS
clipboard and a local Linux clipboard are the same. Bracketed paste is the
portable SSH paste path; Ctrl+V/Pyperclip remains a local fallback.

## Query status and cancellation

The top of the command pane shows compact query cards instead of a verbose
query label. A running query has a rectangular loop with a moving, fading snake segment.
Its top and bottom strokes align with the query card borders using ordinary
terminal line characters, without plugins. Elapsed time refreshes every 0.1 seconds
while running, showing tenths of a second below one minute (for example, `0.1s`
or `12.3s`);
completed, failed, and cancelled queries retain their outcome and elapsed time
after result clearing, pane closing, focus changes, or tab switching. Successful
row-producing queries also retain their displayed row count, using `200+ rows`
when the result was server-truncated or `200 of 1,234 rows` when the total is
known. Durations adapt from sub-second values such as `0.128s` to values such as
`1m 05s`. After five minutes, a running query adds the warning
`consider optimizing your query or sit tight`.

The outlined amber **RUN** button occupies the rightmost position in the
status strip when the current tab is ready. It executes the same SQL selection
or editor contents as Ctrl+Enter, including mutation confirmation. RUN is hidden
while that tab has a queued, running, or cancelling query or another SQL
operation in progress; work on other tabs does not prevent submitting a query
from an idle tab.

The outlined red **STOP** button replaces RUN while a query is running. It is enabled only while a query can be interrupted. Operational
system notices share the status-button row; the command input sits below it.

The `STOP` button and `cancel` command use the same targeted cancellation
path. They identify only the active Explorer user-query label through
`sql.show_queries(...)` and call `sql.cancel_queries(...)` for matching IDs.
Metadata queries are cancelled independently when their cursor context becomes
irrelevant; STOP targets only user operations.
The STOP button is visible only for running user SQL, and remains visible but
disabled while cancellation is pending; the interface remains busy until the SQL worker acknowledges completion
or cancellation.

Escape toggles between the editor and command panel. From a visible result/error
pane, Escape moves to the command panel; use Up and Down at pane boundaries to
enter or leave results/errors. A confirmation or navigation modal consumes Escape
first; Find/Replace and completion overlays close before focus navigation.

## Commands

In the command line, Tab or Ctrl+Space completes command names and supported
argument choices. `db ` completes configured database keys using configuration
validation only, without connecting to databases. Continued typing filters the
menu; Up/Down selects, Tab/Enter accepts, and Escape dismisses it. Accepting a
suggestion does not execute the command: press Enter again to submit.

Enter commands in the lower panel, with or without a leading colon:

- `run` - run the selection or complete editor buffer
- `create_table` - open the table creation form for the current tab
- `format` - format selections or the complete editor using the active SQL dialect
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
- `clear query|results|all` - clear workspace content
- `to_excel` - choose a project directory and save the current result as `.xlsx`
- `to_csv` - choose a project directory and save the current result as `.csv`
- `help` - open the in-app command reference
- `exit`, `quit`, or `q` - close the Explorer, asking about unsaved changes
- `exit!` or `q!` - close without the Save/Don’t Save dialog, discarding unsaved edits
- `wq` - save every changed tab and exit; untitled tabs request a filename and
  directory. A cancelled or failed save keeps the Explorer open.

All exit commands cancel running user queries and wait for their workers to stop.

The confirmation choice and primary run shortcut are saved
in the user's config directory. SQL text is not persisted. The export commands first ask for one
filename with the required suffix, then reuse the project directory browser. An
existing destination requires replacement confirmation. If a result has more than
200 rows, confirm saving all rows before the Explorer reruns a capped query.

In every two-choice confirmation dialog, Left selects the affirmative action and
Right selects cancellation; Enter activates the focused choice. The existing Y,
N, Escape, and mouse controls remain available.

## Creating tables

`create_table` opens a form that calls
[`sql.create_table`](../sql/functions/create_table.md). The database is fixed to
the originating tab's connection key. Enter `table_name` and choose one source:
`table_schema` provides add/remove column-name and SQL-type rows; `from_sql`
provides a multiline query field and maps to the function's `sql` argument.
Type fields offer backend-specific suggestions using Up/Down and Enter,
while accepting custom type expressions.

The basic form always shows `insert_data`, `skip_if_exists`, and
`drop_if_exists`, initially False. `insert_data` is enabled only with source SQL.
`skip_if_exists` maps to `if_not_exists`; selecting skip clears drop and vice versa.
Advanced expands source database, partition/order, and backend-specific creation
settings. Retry controls are omitted; creation uses the function’s retry defaults. Blank advanced settings retain the function defaults;
structured mapping fields accept JSON objects.

Navigate the form with Up/Down and Enter; Tab and Shift+Tab do not move between
controls. Left/Right changes choices or moves the text caret. Enter accepts type
suggestions, opens/accepts choices, toggles checkboxes and Advanced, or activates
buttons. In multiline SQL, Up/Down moves through lines and leaves the field at
the first/last line; Enter inserts a newline. Escape first dismisses an open
choice/type menu, then cancels the form.

**Cancel** or Escape closes the form without executing SQL. **Create** validates
and queues the operation; the button is its execution confirmation. Creation
shares the user-operation queue and status/cancellation handling. Results and
errors belong to the originating tab, and reopening the form retains the last
submitted values for correction. Table and column caches refresh after creation.

[All module docs](../README.md)
