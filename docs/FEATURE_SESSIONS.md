[Documentation overview](README.md)

# Isolated agent feature sessions

### Daily feature sessions

From the canonical checkout, launch a fresh isolated session with bare `codex`
or bare `claude`. Both launchers create and synchronize a private clone before
entering native Plan mode. Complete the plan there, then leave Plan mode for
implementation. Codex and Claude Code sessions are equivalent: the same clone,
preparation, repository MCP server, workflow tools, branch prefix and local
review contract, all generated from `agent_tools/agent_runtimes.py`.

Claude Code starts with `--permission-mode plan` and a launcher-rendered
`--mcp-config` bound to the absolute session clone, with the same MCP tool
timeout as Codex. In Claude Plan mode, repository MCP tools that change state,
such as `prepare_start`, ask for interactive approval before running. Claude Code
asks whether to trust the new clone on its first launch.

Dependencies are downloaded into a shared local wheelhouse on the first launch.
Later sessions with matching Python, platform and requirement inputs install from
that cache into their own `.venv`; the toolkit remains editable from each clone.
Dependency versions change when those inputs change or when you explicitly refresh:

```bash
codex --refresh-bootstrap-cache
claude --refresh-bootstrap-cache
```

A failed preparation prints its ID, private log location and recovery command:

```bash
codex --resume-bootstrap <session-id>
claude --resume-bootstrap <session-id>
```

Run recovery from the canonical checkout. It preserves the original clone and
accepts only a clean `dev` session still in `launching` phase. The clone is refreshed
before its tools run, and Plan mode opens only after successful preparation. A warm
wheelhouse avoids PyPI; cloning and dev synchronization still need Git access.
After updating the launcher, run `python -m agent_tools.install_sessions` from the
prepared canonical environment and source the installed shell file in existing
terminals: `source ~/.local/share/analytics-toolkit-agent/atk_sessions.zsh`.
Earlier installs that source `codex.zsh` keep working; it is now a shim for the
shared file, and the installer never adds a second `source` line.

Use the repository tools from the session clone:

```bash
agent_tools/mcp_tool.sh git-workflow start
# Implement the approved plan and run the recommended focused checks.
agent_tools/mcp_tool.sh run-checks --level precommit
agent_tools/mcp_tool.sh git-workflow commit --message "Describe the feature" --path path/to/changed/file
```

If `start` reports a newer `dev` SHA, inspect its reported changes and reassess
the plan before acknowledging that SHA. Material changes need a revised plan.

Before publication a fresh, read-only reviewer session of the same CLI reviews
the complete diff; set `ATK_LOCAL_REVIEWER=codex` or `ATK_LOCAL_REVIEWER=claude`
to choose the other. This local review is the only source review, so it fails
closed: a missing CLI, isolation mismatch, incomplete diff coverage or anything
but an explicit aligned approval blocks publication. The signed receipt records
`reviewer_runtime`, `reviewer_version`, `reviewer_model` and `reviewer_auth`.

Verified signed PR publication automatically hands ownership to the Pi. Finish
with the PR link without waiting for CI or merge; the Pi owns corrections. Do not
edit handed-off branches. Recover interrupted handoff with `git-workflow handoff`.
Handoff archives the task and refreshes clean dev before another planning session.
Use `git-workflow sync` to retry pending synchronization. Dirty canonical work is
preserved and reported. Start another task in native Plan mode; its task and
changelog fragment are unique even while earlier PRs are open.

Local sessions run focused and pre-commit checks before publication. The private
GitHub agent follows integration runs and repairs failures after merge; do not
start integration stacks locally for this normal feature workflow.

Optional `git-workflow feedback --pr <number>` reads a previous PR's status.
Local synchronization prunes remote references and removes proven merged,
inactive branches reachable from dev. Unpublished, active and unique squash
history is preserved; the Pi cleans verified merged remote feature branches.

[Documentation overview](README.md)
