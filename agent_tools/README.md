# Agent Tools

This directory contains repository-local tools for coding agents working in this
checkout. These tools are not part of the public `analytics-toolkit` package API
and are not installed through package extras.

## MCP Interface

Use the repository-local MCP server as the normal agent interface for startup,
documentation retrieval, workflow status, version bumps, checks, git workflow,
and release workflow entrypoints. For terminal validation and planning, use the
wrapper:

```bash
agent_tools/mcp_tool.sh prepare-start --task "implementation" --module agent_tools
agent_tools/mcp_tool.sh docs "specific topic" --mode search --top-k 3
agent_tools/mcp_tool.sh workflow-status --task "implementation" --module agent_tools --instructions-read
agent_tools/mcp_tool.sh workflow-metrics
agent_tools/mcp_tool.sh change-impact --task "change sql.read" --module sql --symbol sql.read
agent_tools/mcp_tool.sh version-bump "Updated agent workflow" --dry-run
agent_tools/mcp_tool.sh version-bump --change-type release --force-release --dry-run
agent_tools/mcp_tool.sh run-checks --area agent_tools --level focused --dry-run
agent_tools/mcp_tool.sh run-checks --area sql --level integration --dry-run
agent_tools/mcp_tool.sh run-checks --area sql --level integration --integration-profile core
agent_tools/mcp_tool.sh visual-workflow start
agent_tools/mcp_tool.sh visual-workflow capture --review-id <review-id>
agent_tools/mcp_tool.sh visual-workflow status --review-id <review-id>
agent_tools/mcp_tool.sh visual-review --review-id <review-id> --scene-id <scene-id> --verdict pass
agent_tools/mcp_tool.sh visual-workflow complete --review-id <review-id>
agent_tools/mcp_tool.sh git-workflow commit --message "Update agent workflow" --path agent_tools/mcp_server.py --path tests/agent_tools/mcp
agent_tools/mcp_tool.sh git-workflow checks --sha <exact-pushed-sha>
agent_tools/mcp_tool.sh release-workflow --action merge-dev
agent_tools/mcp_tool.sh release-workflow --action status
```

The agent-only MCP runtime requires Python 3.10 or newer and MCP SDK 2. The
committed `.codex/config.toml` registers the server automatically for trusted
project sessions. Bootstrap or refresh `.venv` with `requirements-mcp.txt`, then
restart Codex or reopen the workspace after cloning or changing MCP
configuration; MCP servers are loaded when a session starts. Running
`agent_tools/mcp_tool.sh` without arguments starts the stdio server, while the
commands above retain the manual JSON CLI.

The project registration is optional (`required = false`) so a missing local
environment does not prevent Codex from opening. The launcher never installs
dependencies automatically; bootstrap failures report the command needed to
repair the environment.

MCP tools return compact receipts by default. Successful summary responses omit
internal command records; failures return a structured blocker with one bounded
excerpt. Complete stdout and stderr remain available with private permissions
below `.rag_index/tool_logs/`. Use `--detail diagnostic` for command metadata
after a blocker or `--detail full` only when complete inline output is necessary.
Summary budgets are enforced before receipts are returned. Summary telemetry
contains final size, budget, and truncation state; diagnostic telemetry adds
per-section and raw/suppressed sizes. `workflow-metrics` aggregates the active
startup session and labels its byte-based token estimate because model billing
tokens are unavailable to repository tools.

Startup records an environment fingerprint and reuses a healthy `.venv` while
requirements, project metadata, tox configuration, and the Python runtime are
unchanged. Dependency installation is quiet when the fingerprint changes.

Bare `codex` from the canonical repository creates a private clone on `dev`,
pulls before launch, prepares the repository MCP, and selects native Plan mode
before accepting task input. Install this launcher with
`python -m agent_tools.install_sessions` from the prepared project environment.
Other directories and Codex commands continue to use the existing CLI.

Automatic handling accepts only the repository owner's PRs whose heads are SSH
signed by an enrolled machine, plus this App's signed repair commits. The launcher
creates a device key outside clones; its public key must be enrolled in the
reviewer host's private allowed-signers file. An unknown or revoked key fails
closed, independently of the model verdict.

`prepare_start(..., user_prompt="exact starting message")` records the original
task once under private `.git/atk-tasks/`. Append task corrections with
`task_context(action="append", user_prompt="exact correction")`. Retries and
resumes preserve the original; verified PR handoff archives it before another task. CLI calls
accept `--user-prompt-file` so prompt text does not need shell interpolation.
The PR workflow attaches redacted context and a signed-commit digest. Private
originals never appear in tool receipts. MCP-supplied text relies on the enrolled
machine trust boundary; it is not independent proof of human authorship.

Oversized attachments retain the complete task in a content-addressed Git archive
instead of truncating messages or repeating hidden payloads in the PR body.
The compact attachment links to readable history pages containing the original
prompt, chronological corrections, agent questions and their complete user
answers, plans, decisions and image records. Bounded canonical JSON chunks retain
the exact signed task bytes. Readers verify chunk sizes and SHA-256 hashes,
canonical task identity, the immutable archive layout and the candidate's existing
signed trailers. Legacy inline v1/v2 attachments remain readable.

Archive refs use `refs/tags/agent-task-evidence/v1/<task-digest>` and contain only
task data, outside the candidate tree and its visual receipt. Archives grant no
approval and do not promote package code. Publication preflights the complete PR
body and all archive bounds before mutations, confirms the immutable archive
before the feature push, and rechecks current authority after archive I/O.
An interrupted archive create is recovered by its exact identity; conflicting
refs are never replaced. Keep archive refs for the task's audit history.
The v1 archive layout is a frozen transport contract covered by a historical
fixture; a future layout needs a new version and a retained v1 reader.

Controller code/prompts, security configuration, dispatch/capture/publishing
workflows and the required-check manifest require exact-head owner approval.
Ordinary policies, tools and integration/test workflow repairs are reviewed
against the attached task automatically. Owner can post
`/agent approve-policy <current-head-sha>` or click the authenticated Telegram
approval button. New heads invalidate approval. Local agents cannot approve or
resume on the owner's behalf. PR files cannot enroll keys or change this gate.

After planning, use `git_workflow(action="start")` to pull dev again before a
unique feature branch is created. A changed base requires plan reassessment;
acknowledge only after reviewing the changed plan. Independent clones prevent
concurrent sessions from sharing an index, branch, or working tree.

Feature sessions commit focused and pre-commit checked changes to their own
branch and open a PR to dev. Verified publication automatically hands ownership
to the Pi, archives the task, and refreshes clean dev. Finish with the PR link
without waiting for review, approval, CI or merge. Publication failures retain
local ownership; `handoff` recovers an interrupted handoff and `sync` retries
pending dev refresh. Dirty canonical work is preserved. Each next feature has
its own task ID and changelog fragment even while earlier PRs are pending.
Handed-off branches prohibit local corrections. Optional
`git_workflow(action="feedback", pr=<number>)` reads a selected PR's status.
Local cleanup prunes remote references and deletes only proven merged, unused
branches reachable from dev. Unknown, active and squash-only unique local work
is preserved and reported; the Pi safely cleans verified merged remote branches.
Each reviewer is a fresh read-only session separate from the candidate authors.
Reviewer rejection posts detailed plain-text alerts and starts a fresh Pi
correction agent followed by independent review, repeating until approved.
Only genuine user questions, owner veto/cancel, invalid provenance or subscription
and authentication limits pause work. Rejection messages have no approval buttons.
Fast CI, independent review and protected exact-head owner approval still apply.

The private subscription worker is maintained in self_hosting's
`self_host/services/github_agent`. Airflow calls its Unix socket every minute
and updates the underlying Codex executable Sunday at 04:00 Europe/Moscow.
The Pi performs bounded source operations only; GitHub runs tests and renders
visual scenes. Up to ten jobs run concurrently with serialized merge decisions.
Fast checks and the App's exact-head/base review gate merges. Integration runs
are monitored after merge and repaired through reviewed PRs until matching
scenario groups and transports are green. No OpenAI API key is used.

Recommended implementation choices go to a separate read-only plan resolver,
which approves them only when they align with the complete user request. Genuine
questions and protected exact-head owner decisions retain their authenticated
answer path. A singleton maintenance orchestrator starts at most every thirty
minutes while PRs are open, preserves failed patches and task evidence, and
may propose source repairs in analytics_toolkit and self_host. Independent
review and GitHub validation gate publication and merge; the Pi runs no checks.
An idle trusted supervisor activates newly merged, owner-approved automation
paths on main only after verifying their merge identity, signatures, task
binding, fresh authorization and exact-head/base checks. Package code remains
on dev until an explicit release.

`change-impact` is a read-only preflight for implementation work. For SQL public
symbols it reports the live signature, exact integration-manifest JSON pointers,
signature drift, documentation paths, focused checks, changelog action, and SQL
module line-budget headroom before editing begins.

Use `git-workflow commit` only for a coherent checked batch with explicit paths.
In a feature session it pushes the feature and opens a PR; the current checkout's
legacy dev route remains available for bootstrap. `release-workflow` retains
its explicit main-only release process.

Only an explicit user waiver for the current batch permits
`git-workflow commit --validation-waiver "user-authorized reason"`. This records
the reason in the commit without creating a successful validation receipt; it
does not bypass path safety, signatures, writer leases, or GitHub merge gates.

SQL Explorer production or visual-harness changes also require the private
visual receipt created by `visual-workflow`. The capture runs headlessly on the
current Linux, macOS, or Windows host, using the checkout's Python environment.
It starts each scene in a separate process with Textual's headless driver and
fake database fixtures, exports the complete application screen, and rasterizes
it with `resvg-py` at 2560x1600, then reduces once with Pillow's
`Image.Resampling.LANCZOS` to the full 1280x800 PNG. An outer SVG viewport scales
the unchanged framed SVG; intermediate and final dimensions are validated, and
rendering errors propagate. The terminal grid is 208x47 cells. A host
monospace font must be installed (for example Menlo, Consolas, or Liberation
Mono). No VM, OS image download, desktop session, or VNC server is needed.

Install `agent_tools/requirements-mcp.txt` in the agent environment for the PNG
renderer. The GitHub latest/minimum test matrix and tox latest/minimum and
coverage environments also install `Pillow>=10,<13` for the unconditional,
service-free supersampling regressions.

On Windows, the workflow finds `.venv/Scripts/python.exe`; on Linux and macOS it
uses `.venv/bin/python`, falling back to the running interpreter.
The shell-free CLI equivalent on every platform is:

```text
python agent_tools/mcp_server.py call visual-workflow start
python agent_tools/mcp_server.py call visual-workflow capture --review-id <review-id>
```

Capture copies only reviewable checkout files to a temporary workspace and
removes that workspace afterward. Screenshots contain the application's UI,
not native window decorations. `visual-workflow status` returns the next PNG
batch; the agent must open every full image and record a verdict through
`visual-review`. Geometry checks and every agent verdict must pass before
completion. The receipt records the host and capture method and is bound to
the complete reviewed content; only a current all-pass receipt allows commit
or push. Failed or interrupted captures require a new review session.

Capture waits for both intended scene state and passing geometry, with a bounded
timeout. Save scenes require the active document name and requested button focus;
invalid create-table scenes require an incomplete row and its validation notice.
Completion scenes start unready, open fake suggestions after editor refresh, and
require a mounted, visible popup with composed suggestion text and valid geometry.
A popup hidden during an earlier invalid layout must become visible when its
cursor and available space are valid again.
Normal modal teardown postpones readiness, including delayed configuration and
queued readiness callbacks. Evidence refreshes require a mounted active screen;
only missing-control and screen-stack lifecycle errors are handled, while
unexpected errors still fail.
Immediately before export, capture rechecks readiness, removes stale geometry,
and validates every required control again. Missing controls or a timeout block
PNG export. Configuration is recorded after mounted controls are available and the
intended action has been requested; periodic refreshes then poll settled state
without resubmitting validation. Invalid-scene feedback is read from the Static
plain-text renderable; an unexpected renderable fails instead of passing on its
object representation. Control mounting is checked before reading modal values.
Timeout errors include bounded readiness conditions,
missing or unmounted controls, incomplete rows, notice text, requested focus,
and freshly evaluated geometry assertions before teardown and evidence removal.
The timeout and every required geometry check remain unchanged.

The `directory-armed` fixture configures its pending picker after mounting,
refresh and queued initialization messages, then invokes the existing Escape
action once. Readiness requires directory-selection mode, mounted path/tree,
selection-button and notice controls, the armed flag and button class, selection
button focus, and the exact notice
`Enter: select this directory   Escape: cancel   Arrows: keep browsing`.
Periodic readiness checks observe these conditions without re-arming the picker.
The public harness method `scene_readiness_details()` exposes the existing
readiness contract while retaining `_scene_readiness_details()` for compatibility.
Each directory geometry payload collects readiness once, so its semantic assertions
and recorded scene-state details describe the same observation.
Geometry includes the same semantic assertions and bounded scene-state evidence;
lost focus, styling or instructions blocks capture and appears in timeout
diagnostics. Service-free regressions retain the state across idle refreshes and
verify Enter selection, Escape cancellation and arrow browsing on the real fixture.
These changes require a new full 46-scene capture and independent PNG review;
the initialization timing hypothesis remains unverified until GitHub validation.
Button color assertions retain `button_focus_color:<id>` for named controls.
Controls without IDs use their widget class, sorted CSS classes and query index
(for example, `button_focus_color:Button.remove-column[0]`), so every removal
button is checked separately. Modal buttons use the surface color when neutral
or disabled, the shared hover color under the pointer, and amber for enabled
keyboard focus, including focus with hover. Disabled-color regressions resolve an
independent disabled Button reference with `color: $text-disabled` resolved by
Textual CSS after mounting beside each tested button under the same parent.
Plain-color parsing cannot resolve automatic/percentage CSS values such as
`auto 38%`. The reference preserves Button opacity, ancestor
background and contrast context. They compare CSS foreground/background, automatic
contrast and rendered foreground/background exactly, and reject
an intentionally incorrect reference foreground.

For database-picker, find-replace, column-ruler-scrolled, create-table-advanced,
create-table-schema, create-table-invalid, and create-table-sql, geometry JSON also includes bounded
`render_diagnostics`: widget and composed terminal strips, styles and control
state, SVG clips/transforms, scrollbar state, and frame hashes. Missing or
truncated layers explicitly report incompleteness. The PNG and diagnostic hashes
come from one export; atomic evidence replacement protects that frame from later
timer refreshes. These diagnostics do not replace geometry checks or individual
review of every full PNG.

The `keyboard-multicursor` scene additionally records `terminal.multicursor`:
actual workspace, keyboard mode, focused widget, primary/secondary selection
identities and endpoints, preferences, scroll offsets, gutter and viewport.
Each observed caret maps through the wrapped document to content-local,
widget-local and screen cells. Its text, resolved colors and style flags come
from the already-collected widget/compositor rows; collection does not move
cursors, change focus, drain events or refresh the scene. The first three editor
rows and ruler row share the existing nine-region, 24-row and 208-cell limits.
Caret records are capped at nine. Missing probes, clipping, missing rows/styles
and all collection limits explicitly make the reduced evidence incomplete.
Expected positions `(0,14)`, `(1,14)`, `(2,14)`, primary `(1,14)`, empty
selections and keyboard mode on remain separate from observations. State
mismatches and absent caret marks are findings, not a PNG visibility verdict.

Multicursor `export_input_evidence` retains the exact raw, framed and ordinary
supersampling SVGs from the single final export without comparison renders.
All three inputs share the 4 MiB lossless SVG budget; overflow omits content
while preserving lengths/hashes and reports `complete=false`. Its
`rasterization` record retains the ordinary renderer settings, scale factor,
intermediate/final dimensions and hashes, Pillow version and reduction filter;
`svg_input` identifies `inputs.supersampling_svg` as the retained rendering input.
Verify its final hash and `png_sha256` against the full scene PNG. Parsed styles
also retain SVG text-decoration attributes. Existing parsed-element and 512 KiB
limits, atomic publication, readiness and geometry checks stay in force.
GitHub-only diagnostics support identification of the first failing layer;
they do not repair or clear the visibility finding. A demonstrated repair
needs renewed independent planning and review, exact-head CI and all 46
candidate-bound geometry/full-PNG verdicts.

The `tab-database-change` geometry also records `ruler_evidence`: the active
workspace, cursor and one-based column, integer scroll offsets, gutter width,
separate ruler/editor regions, and the ruler and first editor row from both
widgets and compositor. Each row retains spaces, terminal-cell coordinates and
styles through the existing 208-cell bound. Collection observes the mounted state
without changing focus, scrolling or SQL; final-frame ownership and atomic
publication remain unchanged. Mounted regressions cover tab/database switching
and reject overlapping rows, shifted column mapping and missing composed labels
or gutter numbers. This evidence supports independent reassessment of the ruler
finding; it does not clear a rejected visual gate. Fresh candidate-bound review
must still inspect every one of the 46 full PNGs and geometry results.

Create-table checkbox entries additionally retain bounded model label/value and
enabled/focused state, direct Rich render text/styles/spans, computed geometry and
styles, and ancestor clips. The terminal snapshot records whether Advanced is
collapsed. Direct output remains separate from cached widget and compositor rows;
text loss in either layer is an observation, not a collection failure or a pass.
Missing probes and label, direct-render, ancestor or payload limits explicitly
mark evidence incomplete. These additions share the existing region, row, cell,
element and 512 KiB bounds.

The strict checkbox indicator and complete-label assertions attach a JSON snapshot
only when they fail, using the original collected rows before teardown or another
refresh. The message retains Python, Textual and Rich versions and terminal size;
it never recollects rows or changes readiness to wait for the disputed text.
GitHub must reproduce the advanced scene on Python 3.14/Textual 0.89.1, then the
canonical and minimum environments, and retain exact-head coverage JSON with
missing statements/arcs and tool versions. The first failing checkbox layer and
the four picker comparisons still need independent review before a production
repair is selected. Assertions, coverage/debt baselines and full-scene review
remain required; this diagnostic checkpoint supplies no repair or visual verdict.

Database-picker geometry also retains versioned `export_input_evidence` outside
the 512 KiB parsed-diagnostic reducer. Its raw and framed SVG `content` strings
are the actual export and target frame inputs. The supersampling input is retained
separately in `rasterization_comparisons`. Decode JSON, encode each string
as UTF-8 without normalization, then verify its byte length and SHA256; verify
`png_sha256` against the same frame. Combined SVG bytes are limited to 4 MiB.
Above that limit both contents are omitted, lengths/hashes remain, and
`complete=false` explicitly blocks the diagnostic prerequisite. No content is
silently truncated. The geometry and parsed-layer completeness verdicts remain
independent and must still pass.

Rendering context includes Python, Textual, Rich and resvg-py package versions,
requested SVG font families, and bounded fontconfig inventory/match probes.
Missing commands, timeout, errors and output limits are explicit. System matches
do not establish resvg font selection, which is marked unobservable. These probes
run only in the capture job and do not change fonts, framing or rasterization.
Picker geometry additionally includes versioned `rasterization_comparisons`
outside the parsed reducer: `raw_original`, `framed_original`, `raw_dejavu` and
`framed_dejavu`. All four reuse the single final screenshot export. The framed
original is rendered independently; all seven direct controls call the same resvg
API with only `svg_string`. Raw inputs keep native sizing. Diagnostic font
variants replace only the generated terminal matrix font-family declaration with
DejaVu Sans Mono; title fonts, text, positions, clips and framing stay intact.
An exact, nontruncated fontconfig family/file observation must confirm that family
is installed. This observation does not prove the renderer selected that font.

Each variant retains the exact UTF-8 SVG and base64 PNG, byte lengths, SHA256,
actual PNG dimensions, font/framing identity and renderer arguments; package
versions and the font probe accompany the comparison. Decode SVG strings without
normalization and base64 PNG content without transformation, then verify both
lengths and hashes. Combined comparison SVG bytes and decoded PNG bytes each have
a separate 4 MiB limit (including the reused ordinary variant). Exceeding either
limit omits all content of that type while retaining available lengths/hashes.
Oversized SVG inputs are not rendered. Missing fonts, ambiguous declarations,
render failures and omissions have explicit per-variant reasons and
`complete=false`. Service-free regressions cover both runtime and operating-system
renderer errors while retaining the successful variants. Successful ordinary capture
or parsed diagnostics cannot override incomplete comparisons. Production UI remains
unchanged; probes and comparison rendering execute only on GitHub.
Readiness and final geometry must pass before export or comparison rendering;
regressions verify that stale successful comparisons cannot qualify a rejected frame.

Three additional controls separate serialization from scaling:
`raw_xml_native` reserializes the raw SVG with native sizing;
`framed_preserved_target` uses the target frame with every original SVG interior
byte intact; `framed_xml_native` uses the existing XML frame at the original
viewBox dimensions. They retain original font declarations and share both 4 MiB
comparison budgets with the original four variants. Invalid or missing native
sizing and failed controls explicitly make the comparison incomplete. The target
frame remains unchanged.

The additional `framed_supersampled` variant retains the exact outer SVG, renderer
arguments, scale factor, intermediate dimensions/hash, final dimensions/hash,
Pillow version and reduction filter. It alone reuses the ordinary final PNG;
`framed_original` remains a direct-render control. All eight variants share the
existing separate 4 MiB SVG and decoded-PNG budgets and explicit omission rules.

The coordinator must bind and independently inspect the supersampled picker and
all comparisons, then all 46 full scenes. Supersampling is accepted only when the
actual heading is readable and every geometry check and individual PNG verdict
passes. Incomplete evidence or a successful capture alone supplies no visual
approval.

The type-menu viewport regression attaches an observational JSON snapshot before
its initial visibility assertion, after scrolling, and after positioning. It records
focus and tracked-input identities, matches/index/accepted type, mount/visibility,
scroll offsets, field/menu/viewport geometry, advanced-panel state and dependency
versions. Queue sizes and pending layout/refresh flags are read only when available;
unavailable probes and omissions are explicit. Snapshots are limited to 8 KiB and
attach the original observation to failures without retries, event draining,
refocusing or recollection. Before the assertion sequence, the fixture settles the
expanded layout, focuses and scrolls the field into view without animation, then
checks that its full region is visible with space below. Existing menu visibility,
placement and scroll-out assertions remain intact; production positioning is unchanged.

The quality job always attempts to upload generated `coverage.json` under a unique
run/attempt/head-bound artifact name, including when the coverage ratchet fails.
The upload warns explicitly if no report exists and does not change the preceding
command's exit status. Fresh GitHub reports supply actual missing arcs; historical
coverage totals cannot reconstruct them. The 98.03% branch floor and all debt
baselines remain unchanged.

The schema, invalid and SQL creation scenes also record scoped source-selector
children: `SelectCurrent`, its `Static#label`, and the active arrow, with the
inactive arrow described separately. Diagnostics retain the selected/expected
mode, computed styles, child/content geometry, ancestor clipping and complete
bounded render-cache/compositor rows. `visibility_ok` and explicit visibility
findings are separate from collection completeness; successfully observing blank
text does not approve the selector. The region budget is nine; row, cell, SVG and
512 KiB payload bounds remain in force and omissions remain explicit.

Schema, invalid-schema and SQL scenes require complete selector evidence, the
expected source value, and readable composed label/arrow output contained within
every ancestor clip. These checks gate both current scene readiness and freshly
evaluated final geometry. Source and advanced Boolean selectors place their frame
on the inner `SelectCurrent`, leaving the outer three-row allocation borderless
and unpadded. Positive regressions require complete, visible selector evidence
and passing readiness assertions for all three source scenes; failure regressions
retain observational evidence without approving the frame.
Selector-only create-table fixtures also mount the real `create-table-advanced`
Collapsible and compare its collapsed state with the collected checkbox context;
missing-panel negative controls continue to require explicit incomplete evidence.
Per-region collection shares the scene row budget, including after invisible
regions and contained row-collection failures.

Diagnostic collection remains observational and available for failed frames.
Timeout observations are retained in a sibling `*.timeout.json` file, including
selector rows, even if the error log truncates its summary. Final capture deletes
stale geometry before rechecking readiness and geometry; a previous successful
frame cannot approve a clipped selector. Service-free regressions cover composed
output, containment after resize/scroll, keyboard choices, and deliberate double
framing. GitHub must run these on Textual 0.73.x and 0.89.1 and perform a fresh
46-scene capture and individual full-PNG review before publication.

For private controller visual corrections, collect the complete source patch first
and publish only an isolated `agent-visual/<sha>` temporary capture candidate.
GitHub must capture all 46 manifest scenes at 208x47 cells and full 1280x800 PNGs;
an independent reviewer inspects every full PNG individually. Require every geometry
check and scene verdict to pass in a new receipt bound to the actual candidate,
capture artifacts, task digest, and complete patch. Remove the temporary ref before
publishing the correction to the feature PR. Any source correction invalidates that
receipt and repeats the candidate capture and review sequence; historical receipts
cannot certify a changed patch. After publication, require fast CI, matrix/quality
checks and a separate implementation review for the final published head and current
dev base, preserving signatures and protected exact-head owner approval gates.

Normal `version-bump` calls record a feature fragment or add the supplied
summary under `## Unreleased`; they never bump versions, regardless of entry count.
Documentation-only calls retain their no-bump behavior.

An explicit `release-workflow --action merge-dev` request prepares one new
version through a signed PR to dev, before validation/TestPyPI. Supply
`--summary "Release summary"` when no unreleased entries exist. The optional
`summary` Python/MCP argument is keyword-only; existing action/root arguments
and action names remain unchanged. Re-enter the same action after review and merge.
The structured pending response includes the PR link and continuation instruction.

Private `.git/atk-release.json` state binds the release task, source SHA, version,
branch, PR, candidate SHA/tree, workflow runs, and completed stages. Retries reuse
the candidate and retained verified artifacts; they do not blindly recreate
TestPyPI branches or GitHub releases. Changed dev content requires another
preparation PR and fresh validation. Before pushing a temporary TestPyPI branch,
the intended SHA and pending push are atomically journaled, and the signed commit
is retained under a candidate-specific private local ref. Resume retries that
exact commit if the push never reached origin. A remote ref is adopted only after
checking its single candidate parent, project-name-only metadata change, full
changed-path list, and agreement with any journaled SHA. Legacy state without a
SHA can recover through the same checks before publication reconciliation.
An established ref that disappears blocks recovery, except after a journaled,
fully reconciled leased deletion. Unpublished replacement retains the prepared
version and still requires one fresh reviewed PR. Status never prepares or increments.
Direct release scripts require the same reviewed candidate and validation receipts.
See [release instructions](../agent_docs/release.md) for the complete sequence.

Low-level release-oriented `version-bump` options remain available for metadata
preparation, including `--change-type release --force-release`. A summary can
supply notes for an otherwise empty release. Metadata-only preparation PRs use
the rolled release notes without adding a feature fragment that reopens Unreleased.
Private implementation workers add regressions and edit sources without running
programs, local pre-commit checks, tests, installs, or formatters. The trusted
coordinator verifies task identity, pinned base/head, provenance, protected paths,
and patch applicability before publishing an App-signed correction to the PR.
For ordinary feature corrections, preserve versions and Unreleased notes. If the
task-owned fragment is already folded, keep its note once and its signed history;
do not recreate the consumed fragment. Before validation, verify that coordinator
metadata children also preserve these invariants instead of applying an entry-count bump.
Corrections retain current-dev repair discovery, bounded evidence handling, and
unchanged no-patch retry suppression with their fast mocked regressions.
GitHub then validates the exact corrected head through the focused regressions,
Python matrix, SDK conformance, lint, type, coverage, and artifact gates. Successful
`fast-checks`, fresh independent review, and authenticated exact-head owner policy
approval for protected paths are required before merge; earlier-head results and
approvals cannot substitute. This release feature changes protected publishing
workflow guards and controller changelog folding, so the coordinator must present
PR #7 and its final SHA for `/agent approve-policy <sha>` through the authenticated
owner flow after validation and review; agents cannot grant that approval.

`prepare-start` persists a private startup-context receipt below `.rag_index/`.
`workflow-status` reuses it to return compact branch, dirty-state, diff, check
count, and change information without repeating routing, metadata, or full check
commands. Use `--detail diagnostic` when the complete routing and check plan is
needed, and use the consolidated status instead of separate shell probes.

Docs search defaults to three deduplicated citations with bounded snippets and
returned/considered counts. Ranking scores, source metadata, and the supporting result list for
`ask` mode are available through diagnostic detail.

`run-checks --area sql --level integration` starts the repository-owned
disposable Trino/MinIO/Iceberg/Hive and ClickHouse/Keeper stack, adds Greenplum on
x86_64 hosts, runs the marked integration tests, captures failure logs, and
always removes its containers and volumes. It must not be redirected to
external database endpoints.

Managed pre-commit checks run as separate static, coverage, artifact, and Python
matrix commands, stopping before downstream work when a stage fails. Coverage
is the canonical Python 3.11 test run; the remaining Python 3.8 through 3.14
matrix omits that duplicate and defaults to three parallel tox workers. Set
`PRECOMMIT_PARALLELISM` to a positive integer to tune it. Artifact installs use
fresh virtual environments with a shared `.tox/pip-cache` download cache.

Successful stages receive private 24-hour receipts keyed by the exact working
tree, command definition, toolchain versions, and parallelism. An interrupted
or corrected run reuses only exact matches and reports each stage as executed,
reused, or failed. Managed coverage target increases are reported and accepted
in the same coverage run, while the manual coverage command keeps its
review-and-rerun behavior. The compatibility `--quick` script mode retains its
plain pytest run, but managed pre-commit validation does not duplicate it.
Check failures include every failed stage, failing pytest
node IDs, quality-debt increases, architecture overages, and tox environments
when available. Repeated identical failures return a compact unchanged receipt
pointing to persisted evidence.

The integration entrypoint accepts `--integration-profile core`, `auth`, `all`,
`fault`, or `stress`; `all` is the exhaustive local default. Each selected
profile runs the same collection with ClickHouse HTTP and native transports.
Use `--integration-clickhouse-driver http` or `native` only for focused
diagnosis; `both` is the managed default and enforces collection parity. Core
and auth run as advisory checks on each `dev` push. Fault and stress run nightly or by
manual dispatch. Each profile writes Compose logs, service health, JUnit output,
collection output, query/object diagnostics, and leak reports below
`.integration-artifacts/<profile>/<transport>/` and always tears down its project
containers, networks, and volumes. Greenplum completeness and the no-skips gate
require x86_64; ARM runs are useful but not the complete deterministic matrix.

Feature-session `commit` and `push` return immediately after verified signed PR
handoff; the Pi owns GitHub checks. Legacy bootstrap and release operations watch the
immutable SHA captured immediately before the push. The watcher discovers every
entry in `.github/required-workflows.json`, waits only for `required_push`
workflows, and reports `advisory_push` workflow status without blocking. It polls
Actions jobs only for required workflows, plus shared commit check-runs and
statuses, and returns run/job URLs and
conclusions. It fails when a required check is cancelled, superseded, missing,
failed, or timed out, or when API/authentication fails. Only conditional skips
declared in the manifest are accepted.
When a required run fails, it waits for terminal state and returns failed steps plus a
bounded `gh run view --log-failed` excerpt. Rerun only for demonstrated
infrastructure failure. When resuming after interruption, use
`git-workflow checks --sha ...`; do not inspect the latest branch run instead.
Advisory status returned incidentally is report-only. Do not query advisory jobs,
download their logs or artifacts, diagnose, retry, or repair them during normal
completion, even while required checks run. Record a deferred follow-up using
existing evidence. Investigation requires explicit user scope or release
readiness; observing a failure alone is not authorization.
Each watch call waits for a bounded interval (60 seconds by default), persists
the repository, exact-SHA deadline, and last reported workflow/job/check states
below `.rag_index/`, and returns changed states plus remaining required checks
on the first pending receipt. Unchanged polls omit the repeated check list.
Large first-poll sets return bounded status-only samples
plus total counts. Terminal success returns required conclusions and URLs;
diagnostic/full detail retains the expanded evidence. Successful polling API
payloads and repeated repository discovery are not returned.

`mcp_server.py` exposes the consolidated tool surface:
`prepare_start`, `docs`, `workflow_status`, `workflow_metrics`, `change_impact`, `version_bump`,
`run_checks`, `visual_workflow`, `visual_review`, `git_workflow`, and
`release_workflow`.

## Docs Assistant

`docs_assistant.py` is the implementation behind `prepare_start` RAG indexing
and `docs(...)` retrieval. It indexes `README.md` plus Markdown files under
`docs/`, `agent_docs/`, and `agent_tools/README.md` into `.rag_index/`, then
uses heading-aware chunks and lexical BM25-like scoring to return grounded
passages with citations.

RAG is intentionally agent-only in this repository. The docs assistant is
stdlib-only and must stay outside public package CLI commands and package
extras. It does not use vector stores, hosted LLM SDKs, Ollama, or embedding
models.

The private Telegram worker also accepts `/prompt_agent <description>`, authenticates
the owner (with an owner-only confirmation button for anonymous channel posts),
validates the request in a separate session, and opens a native Codex Plan-mode
exchange through App Server with read-only source tools.
Reply to a current question or use `/reply_agent <request-id> <answer>`; use
`/cancel_agent <request-id>` to cancel. A separate agent must approve the completed
plan against the initial prompt and all user responses before another agent codes.
Questions during coding pause work and return through the same exchange. The Pi
runs no repository checks, builds or renders; GitHub performs validation and a fresh
independent agent reviews the final PR.

The agent-only validation environment constrains `virtualenv>=21.1,<21.5` so
the required Python 3.8 matrix remains runnable. Project setup resolves this
constraint together with tox; it does not change public package dependencies.

## Release candidate recovery

Ordinary entries do not bump versions. An explicit release prepares one new
four-part version through an independently reviewed PR; see
[release instructions](../agent_docs/release.md) for the publishing sequence.

Re-enter the same release action to resume its candidate/version. TestPyPI retries
adopt matching publish.yml workflow_dispatch runs for the exact temporary ref and
SHA. Rejected dispatches are retryable; uncertain or interrupted attempts retain
a five-minute deadline across invocations, then recheck GitHub and index files
before a replacement dispatch. API/index failures block retry. The release-state
lock excludes concurrent dispatchers. A late original dispatch can still produce
a duplicate run. Saved runs and published files require retained matching artifacts.

If publication stops after PR creation, recovery discovers the existing preparation
PR across open and merged states before attempting another push. It verifies the
release task, branch and base, then resumes durable Pi handoff using the original
signed publication SHA. Corrections made by the Pi are never overwritten by the
stale author head. Conflicting receipts, ambiguous PRs, closed unmerged PRs and
GitHub discovery failures block recovery; only confirmed absence permits publication.

A corrected preparation PR that merges with successful exact-head fast-checks
and agent-review keeps the same version, PR and final reviewed notes. Recovery
archives the old state and clears validation receipts and publication bindings.
Current dev must match the final reviewed tree, and package/README versions and
consumed release notes must remain consistent. Old active or uncertain publication,
published artifacts and incompatible refs block adoption into that same PR.
Reviewed metadata is read with surrounding whitespace stripped and one trailing
newline restored per file. An empty `Unreleased` section is accepted when the
versioned release notes remain present and all changelog fragments are consumed.
Newer dev content automatically starts a replacement preparation PR under the
same release task after publication reconciliation. An unpublished, untagged
version is reused with combined notes; published or tag-reserved versions advance
to the next unreserved four-part version. Published artifacts and their refs stay
intact. Journaled generations recover interruptions without duplicate preparation
PRs or manual state deletion, and require fresh review/checks and release validation.
If recovery is interrupted after journaling a replacement workspace, the next
resume clears the predecessor's validation receipts before publishing that workspace.

The toolkit task's companion Telegram formatting plan was delivered separately.
The supplied private operator attestation records task `100fd816ae2f`, Telegram
messages 55–57 (canonical message 57), and plan SHA-256
`dffec0da15ac21935897d533ca0d37e5e18bdea3cb905a8401c36507cac0c03a`.
Its source bases are self_host `f26ec8f4d6def43f04acaa00d303976a7d6b7b04`
and CIAN `ed8efdea19388a7590ff60d4bbe620b098b57bb2`. This is delivery
evidence, not toolkit approval. Companion code and later companion command
extensions are outside this PR; the toolkit formatting handoff covers GitHub-agent
alerts, plans, questions and bridge responses in readable English HTML.

## User documentation wiki exporter

`wiki_sync.py` exports validated user documentation to the separate GitHub wiki.
It uses only the Python standard library; source Markdown remains authoritative.
See [wiki synchronization setup and retries](../docs/WIKI_SYNC.md) for release
activation, credentials, validation, and ownership of generated pages.
