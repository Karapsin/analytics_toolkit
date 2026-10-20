[Documentation overview](README.md)

# Isolated Codex feature sessions

### Daily feature sessions

From the canonical checkout, launch a fresh isolated session with bare `codex`.
The launcher creates and synchronizes a private clone before entering native
Plan mode. Complete the plan there, then leave Plan mode for implementation.

Dependencies are downloaded into a shared local wheelhouse on the first launch.
Later sessions with matching Python, platform and requirement inputs install from
that cache into their own `.venv`; the toolkit remains editable from each clone.
Dependency versions change when those inputs change or when you explicitly refresh:

```bash
codex --refresh-bootstrap-cache
```

A failed preparation prints its ID, private log location and recovery command:

```bash
codex --resume-bootstrap <session-id>
```

Run recovery from the canonical checkout. It preserves the original clone and
accepts only a clean `dev` session still in `launching` phase. The clone is refreshed
before its tools run, and Plan mode opens only after successful preparation. A warm
wheelhouse avoids PyPI; cloning and dev synchronization still need Git access.
After updating the launcher, run `python -m agent_tools.install_sessions` from the
prepared canonical environment and source the installed shell file in existing
terminals: `source ~/.local/share/analytics-toolkit-agent/codex.zsh`.

Use the repository tools from the session clone:

```bash
agent_tools/mcp_tool.sh git-workflow start
# Implement the approved plan and run the recommended focused checks.
agent_tools/mcp_tool.sh run-checks --level precommit
agent_tools/mcp_tool.sh git-workflow commit --message "Describe the feature" --path path/to/changed/file
```

If `start` reports a newer `dev` SHA, inspect its reported changes and reassess
the plan before acknowledging that SHA. Material changes need a revised plan.

Verified signed PR publication automatically hands ownership to the Pi. Finish
with the PR link without waiting for review, approval, CI or merge; the Pi owns
corrections and independently reviews every candidate. Do not edit handed-off
branches. Recover interrupted handoff with `git-workflow handoff`.
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
