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

assert_clean_worktree() {
  if [ -n "$(git status --porcelain)" ]; then
    printf 'Working tree must be clean before publishing.\n' >&2
    git status --short >&2
    exit 1
  fi
}

require_command git

assert_clean_worktree

current_branch="$(git branch --show-current)"
if [ "${current_branch}" != "main" ]; then
  printf 'PyPI releases must run from main; current branch is %s\n' "${current_branch:-<detached>}" >&2
  exit 1
fi

git fetch origin main --tags

head_commit="$(git rev-parse HEAD)"
origin_main_commit="$(git rev-parse origin/main)"
if [ "${head_commit}" != "${origin_main_commit}" ]; then
  printf 'Local main must match origin/main before publishing.\n' >&2
  printf 'HEAD:        %s\n' "${head_commit}" >&2
  printf 'origin/main: %s\n' "${origin_main_commit}" >&2
  exit 1
fi

"${script_dir}/scripts/test_pypi_release.sh"
"${script_dir}/scripts/real_pypi_release.sh"
