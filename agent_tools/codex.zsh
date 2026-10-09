# Repository-scoped interactive launcher. Source this file from ~/.zshrc.
codex() {
  local atk_source="__ATK_SOURCE__"
  local atk_root
  atk_root=$(git rev-parse --show-toplevel 2>/dev/null) || atk_root=""
  if (( $# == 0 )) && [[ "$atk_root" == "$atk_source" ]]; then
    local atk_binary=${commands[codex]}
    [[ -n "$atk_binary" ]] || { print -ru2 -- "Codex CLI is missing"; return 1; }
    PYTHONPATH="$atk_source" "$atk_source/.venv/bin/python" -m agent_tools.plan_terminal "$atk_source" "$atk_binary"
  else
    command codex "$@"
  fi
}
