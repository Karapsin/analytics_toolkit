#!/usr/bin/env bash
set -euo pipefail

script_dir="$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(CDPATH='' cd -- "${script_dir}/.." && pwd)"

cd "${repo_root}"

mode="${1:---all}"
if [ "${mode}" != "--quick" ] && [ "${mode}" != "--full" ] && [ "${mode}" != "--all" ]; then
  printf 'Usage: %s [--quick|--full|--all]\n' "$0" >&2
  exit 2
fi

require_command() {
  local command_name="$1"

  if ! command -v "${command_name}" >/dev/null 2>&1; then
    printf 'Required command not found: %s\n' "${command_name}" >&2
    exit 1
  fi
}

run_stage() {
  local stage_name="$1"
  local status
  shift

  printf '::agent-check-stage::%s::start::running\n' "${stage_name}"
  if "$@"; then
    printf '::agent-check-stage::%s::end::passed\n' "${stage_name}"
    return 0
  else
    status="$?"
    printf '::agent-check-stage::%s::end::failed\n' "${stage_name}" >&2
    return "${status}"
  fi
}

pyenv_python() {
  local version="$1"
  local prefix
  local python_path

  if ! prefix="$(pyenv prefix "${version}" 2>/dev/null)"; then
    printf 'Required pyenv Python version is not installed: %s\n' "${version}" >&2
    printf 'Install it with: pyenv install %s\n' "${version}" >&2
    exit 1
  fi

  python_path="${prefix}/bin/python"
  if [ ! -x "${python_path}" ]; then
    printf 'Python executable not found or not executable: %s\n' "${python_path}" >&2
    exit 1
  fi

  printf '%s\n' "${python_path}"
}

export PYTHONPYCACHEPREFIX=/tmp/utils_dev_pycache
# pip 25.0.1 is the final virtualenv seed line that supports Python 3.8.
export VIRTUALENV_PIP=25.0.1

run_quick_gate() {
  require_command python
  require_command tox
  run_stage package-metadata "${script_dir}/scripts/check_package_metadata.sh"
  run_stage readme-dependencies "${script_dir}/scripts/check_readme_dependencies.sh"
  run_stage minimum-constraints python -m release_routines.lib.check_minimum_constraints
  run_stage docs-coverage "${script_dir}/scripts/check_docs_coverage.sh"
  run_stage docs-links "${script_dir}/scripts/check_docs_links.sh"
  run_stage compileall python -m compileall analytics_toolkit tests
  run_stage pytest pytest -q
  run_stage tox-quick tox -e lint,type
}

run_full_gate() {
  require_command pyenv
  require_command tox
  export PYTHON38
  export PYTHON39
  export PYTHON310
  export PYTHON311
  export PYTHON312
  export PYTHON313
  export PYTHON314

  PYTHON38="$(pyenv_python 3.8.18)"
  PYTHON39="$(pyenv_python 3.9.25)"
  PYTHON310="$(pyenv_python 3.10.20)"
  PYTHON311="$(pyenv_python 3.11.15)"
  PYTHON312="$(pyenv_python 3.12.13)"
  PYTHON313="$(pyenv_python 3.13.13)"
  PYTHON314="$(pyenv_python 3.14.5)"

  run_stage tox-full tox -e coverage,artifacts,py38-latest,py38-min,py39-latest,py310-latest,py311-latest,py312-latest,py313-latest,py314-latest
}

if [ "${mode}" = "--quick" ] || [ "${mode}" = "--all" ]; then
  run_quick_gate
fi
if [ "${mode}" = "--full" ] || [ "${mode}" = "--all" ]; then
  run_full_gate
fi
