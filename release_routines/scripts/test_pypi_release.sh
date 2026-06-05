#!/usr/bin/env bash
set -euo pipefail

script_dir="$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(CDPATH='' cd -- "${script_dir}/../.." && pwd)"

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
    printf 'Working tree must be clean before creating a TestPyPI release branch.\n' >&2
    git status --short >&2
    exit 1
  fi
}

assert_job_conclusion() {
  local run_id="$1"
  local job_name="$2"
  local expected_conclusion="$3"
  local conclusion

  conclusion="$(gh run view "${run_id}" --json jobs --jq ".jobs[] | select(.name == \"${job_name}\") | .conclusion")"
  if [ "${conclusion}" != "${expected_conclusion}" ]; then
    printf 'Unexpected GitHub Actions job conclusion for "%s": expected %s, got %s\n' "${job_name}" "${expected_conclusion}" "${conclusion:-<missing>}" >&2
    exit 1
  fi
}

verify_testpypi_artifact() {
  local version="$1"

  (
    set -euo pipefail
    local install_attempt
    local temp_dir
    local venv_dir

    temp_dir="$(mktemp -d /tmp/analytics_toolkit_testpypi.XXXXXX)"
    venv_dir="${temp_dir}/venv"
    trap 'rm -rf "${temp_dir}"' EXIT

    python -m venv "${venv_dir}"
    "${venv_dir}/bin/python" -m pip install --upgrade pip
    for install_attempt in {1..10}; do
      if "${venv_dir}/bin/python" -m pip install --no-cache-dir \
        --index-url https://test.pypi.org/simple/ \
        --extra-index-url https://pypi.org/simple/ \
        "karapsin-analytics-toolkit==${version}"; then
        break
      fi
      if [ "${install_attempt}" -eq 10 ]; then
        exit 1
      fi
      printf 'Install attempt %s failed; retrying after TestPyPI propagation delay\n' "${install_attempt}" >&2
      sleep 30
    done

    cd "${temp_dir}"
    "${venv_dir}/bin/python" <<'PY'
import pathlib
import analytics_toolkit

package_path = pathlib.Path(analytics_toolkit.__file__).resolve()
if "site-packages" not in package_path.parts:
    raise SystemExit(f"analytics_toolkit imported from non-site-packages path: {package_path}")
print(f"Verified analytics_toolkit import from {package_path}")
PY
  )
}

require_command gh
require_command git
require_command python

gh auth status >/dev/null
assert_clean_worktree

original_branch="$(git branch --show-current)"
if [ -z "${original_branch}" ]; then
  printf 'Run this script from a branch, not a detached HEAD.\n' >&2
  exit 1
fi

version="$(python -m release_routines.lib.project_metadata read-field version)"
project_name="$(python -m release_routines.lib.project_metadata read-field name)"
if [ "${project_name}" != "analytics-toolkit" ]; then
  printf 'Expected production project name analytics-toolkit, got %s\n' "${project_name}" >&2
  exit 1
fi
"${script_dir}/check_package_metadata.sh"

branch_name="testpypi-${version}"
base_commit="$(git rev-parse HEAD)"

restore_original_branch() {
  git switch "${original_branch}" >/dev/null 2>&1 || true
}
trap restore_original_branch EXIT

if git show-ref --verify --quiet "refs/heads/${branch_name}"; then
  git branch -D "${branch_name}"
fi

git switch -c "${branch_name}" "${base_commit}"
python -m release_routines.lib.testpypi_metadata replace-project-name "karapsin-analytics-toolkit"
git add pyproject.toml
git commit -m "Use TestPyPI package name for ${version}"
git push --force-with-lease origin "${branch_name}"

gh workflow run publish.yml --ref "${branch_name}"
sleep 5

run_id="$(gh run list --workflow publish.yml --branch "${branch_name}" --event workflow_dispatch --limit 1 --json databaseId --jq '.[0].databaseId')"
if [ -z "${run_id}" ] || [ "${run_id}" = "null" ]; then
  printf 'Could not find workflow_dispatch run for branch %s\n' "${branch_name}" >&2
  exit 1
fi

gh run watch "${run_id}" --exit-status
assert_job_conclusion "${run_id}" "build distribution" "success"
assert_job_conclusion "${run_id}" "publish to TestPyPI" "success"
assert_job_conclusion "${run_id}" "publish to PyPI" "skipped"

verify_testpypi_artifact "${version}"
printf 'TestPyPI release verified for version %s\n' "${version}"
