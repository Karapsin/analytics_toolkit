#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python_bin="${MCP_PYTHON:-${repo_dir}/.venv/bin/python}"

if [[ ! -x "${python_bin}" ]]; then
  python_bin="python"
fi

if [[ "$#" -eq 0 ]]; then
  exec "${python_bin}" "${repo_dir}/agent_tools/mcp_server.py"
fi

exec "${python_bin}" "${repo_dir}/agent_tools/mcp_server.py" call "$@"
