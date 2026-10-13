#!/usr/bin/env bash
set -euo pipefail

script_dir="$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(CDPATH='' cd -- "${script_dir}/.." && pwd)"

cd "${repo_root}"

mode="${1:---all}"
if [ "${mode}" != "--static" ] && [ "${mode}" != "--coverage" ] && \
  [ "${mode}" != "--artifacts" ] && [ "${mode}" != "--matrix" ] && \
  [ "${mode}" != "--quick" ] && [ "${mode}" != "--full" ] && \
  [ "${mode}" != "--all" ]; then
  printf 'Usage: %s [--static|--coverage|--artifacts|--matrix|--quick|--full|--all]\n' "$0" >&2
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

resolve_python_minor() {
  local version="$1"
  local installed_version
  if command -v "python${version}" >/dev/null 2>&1; then
    command -v "python${version}"
    return
  fi
  if command -v pyenv >/dev/null 2>&1 && installed_version="$(pyenv latest "${version}" 2>/dev/null)"; then
    pyenv_python "${installed_version}"
    return
  fi
  if [ -x "${repo_root}/.venv/bin/uv" ]; then
    "${repo_root}/.venv/bin/uv" python find --no-project --no-python-downloads "${version}"
    return
  fi
  printf 'Required Python minor is not installed: %s; install it or set PYTHON%s.\n' "${version}" "${version//./}" >&2
  return 1
}

export PYTHONPYCACHEPREFIX=/tmp/utils_dev_pycache
export PIP_CACHE_DIR="${PIP_CACHE_DIR:-${repo_root}/.tox/pip-cache}"

run_static_gate() {
  local compile_status=0
  local static_status=0

  require_command python
  require_command tox
  run_stage package-metadata "${script_dir}/scripts/check_package_metadata.sh" || static_status=1
  run_stage readme-dependencies "${script_dir}/scripts/check_readme_dependencies.sh" || static_status=1
  run_stage minimum-constraints python -m release_routines.lib.check_minimum_constraints || static_status=1
  run_stage python-support python -m release_routines.lib.python_support check || static_status=1
  run_stage docs-coverage "${script_dir}/scripts/check_docs_coverage.sh" || static_status=1
  run_stage docs-links "${script_dir}/scripts/check_docs_links.sh" || static_status=1
  run_stage compileall python -m compileall analytics_toolkit atk tests || compile_status=1
  [ "${compile_status}" -eq 0 ] || static_status=1
  configure_python_matrix
  run_stage tox-static tox -e lint,type || static_status=1
  return "${static_status}"
}

configure_python_matrix() {
  local version
  local variable_name
  local python_path
  local selected_versions
  if [ "${1:-all}" = "coverage" ]; then
    selected_versions="$(python -m release_routines.lib.python_support coverage-pythons)"
  else
    selected_versions="$(python -c 'from pathlib import Path; from release_routines.lib.python_support import load_policy, versions; print(" ".join(versions(load_policy(Path.cwd()))))')"
  fi
  for version in ${selected_versions}; do
    variable_name="PYTHON${version//./}"
    python_path="${!variable_name:-}"
    if [ -z "${python_path}" ]; then
      python_path="$(resolve_python_minor "${version}")"
    fi
    export "${variable_name}=${python_path}"
  done
}

configure_canonical_python() {
  local version
  local variable_name
  local python_path
  version="$(python -m release_routines.lib.python_support coverage)"
  variable_name="PYTHON${version//./}"
  python_path="${!variable_name:-}"
  if [ -z "${python_path}" ]; then
    python_path="$(resolve_python_minor "${version}")"
  fi
  export "${variable_name}=${python_path}"
}

run_coverage_gate() {
  require_command tox
  configure_python_matrix coverage
  run_stage tox-coverage tox -e coverage
}

run_artifact_gate() {
  require_command tox
  configure_canonical_python
  run_stage tox-artifacts tox -e artifacts
}

run_matrix_gate() {
  local parallelism="${PRECOMMIT_PARALLELISM:-3}"

  require_command tox
  if ! [[ "${parallelism}" =~ ^[1-9][0-9]*$ ]]; then
    printf 'PRECOMMIT_PARALLELISM must be a positive integer; got: %s\n' "${parallelism}" >&2
    return 2
  fi
  configure_python_matrix
  run_stage tox-matrix tox run-parallel -p "${parallelism}" -e "$(python -m release_routines.lib.python_support local-matrix)"
}

run_quick_gate() {
  local quick_status=0

  run_static_gate || quick_status=1
  run_stage pytest pytest -q || quick_status=1
  return "${quick_status}"
}

run_full_gate() {
  run_coverage_gate
  run_artifact_gate
  run_matrix_gate
}

if [ "${mode}" = "--static" ]; then
  run_static_gate
elif [ "${mode}" = "--coverage" ]; then
  run_coverage_gate
elif [ "${mode}" = "--artifacts" ]; then
  run_artifact_gate
elif [ "${mode}" = "--matrix" ]; then
  run_matrix_gate
elif [ "${mode}" = "--quick" ]; then
  run_quick_gate
elif [ "${mode}" = "--full" ]; then
  run_full_gate
else
  run_static_gate
  run_full_gate
fi
