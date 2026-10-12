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
agent_tools/mcp_tool.sh git-workflow feedback
```

If `start` reports a newer `dev` SHA, inspect its reported changes and reassess
the plan before acknowledging that SHA. Material changes need a revised plan.

Wait for GitHub feedback after publishing the PR. If the bot wrote corrections,
run `git-workflow refresh` once its writer lease is released, then make any
requested changes and validate again. Recheck feedback until the PR is merged.
Use `git-workflow sync` to refresh the session's `dev` and the clean canonical
checkout before planning the next feature. A dirty canonical checkout is
preserved and reported for later synchronization.

Local sessions run fast tests and wait for review and fast CI. The private
GitHub agent follows integration runs and repairs failures after merge; do not
start integration stacks locally for this normal feature workflow.

[Documentation overview](README.md)
