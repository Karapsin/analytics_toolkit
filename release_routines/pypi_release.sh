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

read_project_field() {
  local field_name="$1"

  python - "${field_name}" <<'PY'
import pathlib
import sys

field_name = sys.argv[1]
try:
    import tomllib
except ModuleNotFoundError:
    tomllib = None

if tomllib is not None:
    with open("pyproject.toml", "rb") as pyproject_file:
        project = tomllib.load(pyproject_file)["project"]
    print(project[field_name])
    raise SystemExit(0)

in_project = False
for line in pathlib.Path("pyproject.toml").read_text(encoding="utf-8").splitlines():
    stripped = line.strip()
    if stripped == "[project]":
        in_project = True
        continue
    if in_project and stripped.startswith("[") and stripped.endswith("]"):
        break
    if in_project and stripped.startswith(f"{field_name} = "):
        value = stripped.split("=", 1)[1].strip()
        if len(value) >= 2 and value[0] == value[-1] == '"':
            print(value[1:-1])
            raise SystemExit(0)
        raise SystemExit(f"Unsupported pyproject value for {field_name}: {value}")

raise SystemExit(f"Could not find [project].{field_name} in pyproject.toml")
PY
}

assert_clean_worktree() {
  if [ -n "$(git status --porcelain)" ]; then
    printf 'Working tree must be clean before publishing to PyPI.\n' >&2
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

verify_pypi_artifact() {
  local version="$1"

  (
    set -euo pipefail
    local install_attempt
    local temp_dir
    local venv_dir

    temp_dir="$(mktemp -d /tmp/analytics_toolkit_pypi.XXXXXX)"
    venv_dir="${temp_dir}/venv"
    trap 'rm -rf "${temp_dir}"' EXIT

    python -m venv "${venv_dir}"
    "${venv_dir}/bin/python" -m pip install --upgrade pip
    for install_attempt in {1..10}; do
      if "${venv_dir}/bin/python" -m pip install --no-cache-dir "analytics-toolkit==${version}"; then
        break
      fi
      if [ "${install_attempt}" -eq 10 ]; then
        exit 1
      fi
      printf 'Install attempt %s failed; retrying after PyPI propagation delay\n' "${install_attempt}" >&2
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

delete_testpypi_branches() {
  local remote_branch

  while IFS= read -r branch_name; do
    [ -n "${branch_name}" ] || continue
    git branch -D "${branch_name}"
  done < <(git branch --list 'testpypi-*' --format='%(refname:short)')

  while IFS= read -r remote_branch; do
    [ -n "${remote_branch}" ] || continue
    git push origin --delete "${remote_branch}"
  done < <(git ls-remote --heads origin 'testpypi-*' | awk '{ sub("refs/heads/", "", $2); print $2 }')
}

require_command gh
require_command git
require_command python

gh auth status >/dev/null
assert_clean_worktree

current_branch="$(git branch --show-current)"
if [ "${current_branch}" != "main" ]; then
  printf 'Production PyPI releases must run from main; current branch is %s\n' "${current_branch:-<detached>}" >&2
  exit 1
fi

version="$(read_project_field version)"
project_name="$(read_project_field name)"
if [ "${project_name}" != "analytics-toolkit" ]; then
  printf 'Expected production project name analytics-toolkit, got %s\n' "${project_name}" >&2
  exit 1
fi

git fetch origin main --tags

head_commit="$(git rev-parse HEAD)"
origin_main_commit="$(git rev-parse origin/main)"
if [ "${head_commit}" != "${origin_main_commit}" ]; then
  printf 'Local main must match origin/main before production release.\n' >&2
  printf 'HEAD:        %s\n' "${head_commit}" >&2
  printf 'origin/main: %s\n' "${origin_main_commit}" >&2
  exit 1
fi

tag_name="v${version}"
if git rev-parse "${tag_name}" >/dev/null 2>&1; then
  tag_commit="$(git rev-list -n 1 "${tag_name}")"
  if [ "${tag_commit}" != "${head_commit}" ]; then
    printf 'Tag %s already exists at %s, not current HEAD %s\n' "${tag_name}" "${tag_commit}" "${head_commit}" >&2
    exit 1
  fi
else
  git tag "${tag_name}"
fi

git push origin "${tag_name}"

if gh release view "${tag_name}" >/dev/null 2>&1; then
  printf 'GitHub release %s already exists; refusing to recreate a production publish event.\n' "${tag_name}" >&2
  exit 1
fi

gh release create "${tag_name}" --title "${tag_name}" --notes "Release ${version}"
sleep 5

run_id="$(gh run list --workflow publish.yml --event release --limit 10 --json databaseId,headSha --jq ".[] | select(.headSha == \"${head_commit}\") | .databaseId" | head -n 1)"
if [ -z "${run_id}" ] || [ "${run_id}" = "null" ]; then
  printf 'Could not find release workflow run for tag %s\n' "${tag_name}" >&2
  exit 1
fi

gh run watch "${run_id}" --exit-status
assert_job_conclusion "${run_id}" "build distribution" "success"
assert_job_conclusion "${run_id}" "publish to TestPyPI" "skipped"
assert_job_conclusion "${run_id}" "publish to PyPI" "success"

verify_pypi_artifact "${version}"
delete_testpypi_branches
printf 'PyPI release verified for version %s\n' "${version}"
