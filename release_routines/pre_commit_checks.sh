#!/usr/bin/env bash
set -euo pipefail

script_dir="$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(CDPATH='' cd -- "${script_dir}/.." && pwd)"

cd "${repo_root}"

require_command() {
  local command_name="$1"

  if ! command -v "${command_name}" >/dev/null 2>&1; then
    printf 'Required command not found: %s\n' "${command_name}" >&2
    exit 1
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

require_command python
require_command pyenv
require_command tox

export PYTHON38
export PYTHON39
export PYTHON310
export PYTHON311
export PYTHON312
export PYTHON313
export PYTHON314
export PYTHONPYCACHEPREFIX=/tmp/utils_dev_pycache

PYTHON38="$(pyenv_python 3.8.18)"
PYTHON39="$(pyenv_python 3.9.25)"
PYTHON310="$(pyenv_python 3.10.20)"
PYTHON311="$(pyenv_python 3.11.15)"
PYTHON312="$(pyenv_python 3.12.13)"
PYTHON313="$(pyenv_python 3.13.13)"
PYTHON314="$(pyenv_python 3.14.5)"

python -m compileall analytics_toolkit tests
pytest -q
tox -e py38-latest,py38-min,py39-latest,py310-latest,py311-latest,py312-latest,py313-latest,py314-latest
