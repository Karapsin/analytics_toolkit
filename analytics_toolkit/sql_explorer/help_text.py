"""Compact command reference and specialized keyboard references."""

HELP_TEXT = """SQL Explorer commands
  Query: run, cancel, format, create_table, clear query|results|all
  Files/tabs: open, save, connections, db KEY, to_csv, to_excel
  Editing: cp, pst, del; help movement for navigation and selection
  Layout: results switch, results expand N, results shrink N
  Keyboard: keyboard on|off|toggle (F8); F6 switches panes
  Settings: shortcut KEY|reset, confirm on|off|toggle
  Modes: mode exploratory|navigation
  Exit: exit / quit / q, exit! / q!, wq

  help shortcuts   editing, execution, search, and pane shortcuts
  help movement    complete movement and selection reference
Commands are case-sensitive; an optional leading colon is accepted.
"""

SHORTCUTS_HELP = """Non-movement shortcuts
  F6 / Shift+F6              next / previous pane
  Escape                    close overlay, collapse cursors, then toggle editor/command
  F8                        toggle editable keyboard mode (mouse editing disabled)
  keyboard on|off|toggle     set mode; keyboard reports its current state
  Ctrl/Cmd+Enter, F5         run query (Fn+Enter when forwarded as keypad Enter)
  shortcut KEY|reset        customize run key; F5 remains available
  Ctrl/Cmd+A                select all
  Ctrl/Cmd+C / X / V         copy / cut / paste
  Ctrl/Cmd+Z                undo
  Ctrl/Cmd+Shift+Z, Ctrl+Y   redo
  Ctrl/Cmd+F                find/replace; Escape closes
  Tab / Ctrl+Space          SQL or command completion; Tab otherwise indents
  Shift+Tab                 SELECT columns or unindent
  select + Space + Tab      insert * followed by one space
  select * + Space + Tab    insert newline and from, retaining indentation
  Up/Down in commands       command history; newest restores your draft
  Up/Down in completion     choose suggestion; Tab/Enter accepts, Escape closes
  ( [ { ' \" `               paired insertion; wrap selections
  Closing character         skip matching closer; Backspace deletes an empty pair
  Ctrl/Cmd+O / S / N         open / save / new SQL file
  Ctrl/Cmd+T / W             new / close workspace tab
  Ctrl+Tab / Ctrl+Shift+Tab  next / previous tab
  Ctrl+PageDown / PageUp     next / previous tab
  Delete in results         close results
  results switch            horizontal (default) / vertical results
  results expand/shrink N   resize in rows (horizontal) or columns (vertical)
  Mouse separator drag      resize when keyboard mode is off
  run / cancel              execute / cancel query
  cp / pst                  copy selections or buffer / paste at every cursor
  del                       Delete at every cursor, or remove selected text
  clear query|results|all   clear content
  to_csv / to_excel         export results
  help movement             movement and selection commands
Command keys share Ctrl behavior when the terminal forwards them.
"""

MOVEMENT_HELP = """Movement and selection (case-sensitive; coordinates are 1-based)
  S = first row/column; E = last row or physical end of the relevant line
  mv [A [B]]        move to row A, column B; omitted coordinates default to 1
                    adds up to 100 missing rows and 10 missing columns per command
  mv s / mv e       start / end of current line
  mv n/p [N]        Ctrl+Right/Left, N times (default 1)
  s A B C D         absolute inclusive selection: row A col B through row C col D
  mvs A B           select from CURRENT main caret through destination A B
  mvs s/e or S/E    select from current caret to line start/end
  mvs n/p [N]       Ctrl+Shift+Right/Left, N times
  pd/pu [N]         Page Down/Up, N times
  d/u [N]           down/up N logical lines, retaining intended column
  start / end       move and scroll to document start/end
  cursor N u/d      add N cursors above/below; original stays main

  mv 10 5           row 10, column 5
  mv S S            document start
  mv E E            document end
  mv 15 E           end of row 15
  mv n 3 / mv p 2   three words forward / two backward
  s 5 3 10 8        inclusive absolute range
  s S S E E         entire document
  mvs 20 5          current caret through row 20, column 5
  mvs s / mvs e     current caret to line start/end
  mvs n 3 / mvs p 2 select forward three words / backward two
  mvs S S / mvs E E current caret to document start/end
  pd 3 / pu 2       three pages down / two up
  d 10 / u 5        ten lines down / five up
  cursor 3 u        add three cursors above
  cursor 5 d        add five cursors below

Relative moves affect all cursors; absolute moves and selections use the main
cursor and collapse extras. Short lines clamp carets; longer lines restore the
intended column. Left/Right aligns all cursors without multicursor line wrapping.
d/u/pd/pu clamp at document boundaries and never insert lines or edit text.
Only absolute mv adds text; its 100-new-line and 10-new-column caps clamp targets.
Arrows/Home/End and Ctrl+Left/Right move; Shift variants select.
Shift+Up/Down adds/removes cursors; Ctrl+Home/End moves to document bounds.
"""
