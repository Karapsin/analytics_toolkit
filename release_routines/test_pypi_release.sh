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

replace_project_name() {
  local new_name="$1"

  python - "${new_name}" <<'PY'
import pathlib
import sys

new_name = sys.argv[1]
path = pathlib.Path("pyproject.toml")
lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
in_project = False
replaced = False

for index, line in enumerate(lines):
    stripped = line.strip()
    if stripped == "[project]":
        in_project = True
        continue
    if in_project and stripped.startswith("[") and stripped.endswith("]"):
        break
    if in_project and line.startswith("name = "):
        lines[index] = f'name = "{new_name}"\n'
        replaced = True
        break

if not replaced:
    raise SystemExit("Could not find [project].name in pyproject.toml")

path.write_text("".join(lines), encoding="utf-8")
PY
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

version="$(read_project_field version)"
project_name="$(read_project_field name)"
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
replace_project_name "karapsin-analytics-toolkit"
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
