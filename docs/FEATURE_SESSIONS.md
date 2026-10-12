[Documentation overview](README.md)

# Isolated Codex feature sessions

### Daily feature sessions

From the canonical checkout, launch a fresh isolated session with bare `codex`.
The launcher creates and synchronizes a private clone before entering native
Plan mode. Complete the plan there, then leave Plan mode for implementation.

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
