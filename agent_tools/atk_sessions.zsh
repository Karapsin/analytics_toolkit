# Repository-scoped interactive launchers for Codex and Claude Code. Source from ~/.zshrc.
# Both wrappers share one interception rule; tests/repository/agent_runtime_parity.py
# keeps them equivalent.
_atk_launch() {
  local atk_runtime=$1
  shift
  local atk_source="__ATK_SOURCE__"
  local atk_root
  atk_root=$(git rev-parse --show-toplevel 2>/dev/null) || atk_root=""
  if [[ "$atk_root" == "$atk_source" ]] && {
    (( $# == 0 )) || [[ "$1" == --resume-bootstrap || "$1" == --refresh-bootstrap-cache ]]
  }; then
    local atk_binary=${commands[$atk_runtime]}
    [[ -n "$atk_binary" ]] || { print -ru2 -- "$atk_runtime CLI is missing"; return 1; }
    PYTHONPATH="$atk_source" "$atk_source/.venv/bin/python" -m agent_tools.plan_terminal \
      --runtime "$atk_runtime" "$atk_source" "$atk_binary" "$@"
  else
    command "$atk_runtime" "$@"
  fi
}
codex() { _atk_launch codex "$@"; }
claude() { _atk_launch claude "$@"; }
