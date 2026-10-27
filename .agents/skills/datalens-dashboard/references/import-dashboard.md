# Import an existing dashboard into a new local project

Use the installed package command, which captures the published dependency
closure, converts supported configuration, retains unsupported readable source,
and generates the launcher. It performs no cloud writes, SQL queries or Editor
script execution. The existing Python environment must provide analytics-toolkit
and datalens-sdk. Do not create an environment or install dependencies for a
consumer project. `python -m analytics_toolkit.cli` selects that interpreter's
package; `atk` is equivalent when its interpreter is known.

## Identify the source and existing authentication

If given a project path, inspect its AGENTS.md, README and configuration to find
the dashboard URL/ID and authentication references. Read deployment constants in
its thin launcher only if needed to locate those values; do not execute it or
reuse its custom export/import scripts. Never read `.connections`, token files,
or credential contents. Reuse the existing organization, YC profile and binary
path. Check `DATALENS_YC_BIN` or `command -v yc` when the existing deployment does
not specify an executable. For Enterprise reuse the base URL and token environment
variable **name**. Report missing configuration rather than creating credentials.

If the caller supplies a Python interpreter, PYTHONPATH or runtime path, retain
them for every command. Module/version identity may be checked with ordinary
Python imports; this is not an extraction script. No SDK skill or repository
implementation reading is necessary for this package-level workflow.

## Preview and write

The destination must be absent or empty. Its parent may contain evidence and
runtime directories. From the destination (create the empty directory if needed):

```sh
python -m analytics_toolkit.cli datalens import-dashboard 'DASHBOARD_URL_OR_ID' \
  --destination . --runtime-dir ../runtime \
  --organization-id EXISTING_ORG --yc-profile EXISTING_PROFILE \
  --yc-binary /existing/path/to/yc --json > ../evidence/preview.json
```

For Enterprise substitute `--installation enterprise --base-url EXISTING_URL
--token-env EXISTING_TOKEN_VARIABLE`. YC settings can alternatively come from
`DATALENS_ORG_ID`, `DATALENS_YC_PROFILE`, `DATALENS_YC_BIN`. A runtime argument or
`DATALENS_RUNTIME_DIR` must point outside the destination.

Every tab, including hidden tabs, is included by default. A `?tab=` in the URL
does not limit the import. Use repeatable `--tab TAB_ID` only when the user asks
for a subset. Subset captures preserve the whole dashboard source but block its
whole-dashboard writer because unselected dependencies were not imported.

Inspect the JSON report, then repeat the same command with `--resume --write`
when the user has requested the local clone. No additional confirmation is
needed for that authorized local write. Preview keeps exact-revision captures
in the external runtime; resume rechecks inventories and reuses matching cached
objects. Progress, requests and cache hits go to stderr; JSON goes to stdout.
Exit 0 means complete capture, 2 means a project/report with capture gaps, and 1
means the operation failed. `written` confirms whether files were published.

If an SDK failure exhausts retries, stop that run and retain its typed error,
request ID and `runtime/import-failure.json`. Do not add a retry loop or raw SDK
fallback. A later explicit rerun can use `--resume`; it revalidates revisions.
Never delete completed capture caches to restart a failed run.

## Inspect and verify

The generated project contains ordinary object/UI configuration, editable
assets where supported, preserved source JSON and original SQL/Editor assets,
`configs/import.json`, coverage, README, and a thin `dashboard.py` launcher.
Private SDK caches, reports and checkpoints stay in the runtime directory.
Run the generated launcher with the same interpreter and external runtime:

```sh
python dashboard.py validate --json > ../evidence/validate.json
python dashboard.py verify --full --json > ../evidence/verify.json
```

`validate` checks offline configuration and captured-source/asset integrity.
`verify` checks live published metadata, source and supported recipe semantics.
`status --json` is available for a later metadata/plan inspection; it is redundant
when performing full import verification. Run cloud commands **sequentially**:
wait for each process to finish before starting another, including preview,
write, status and verify. Do not batch separate CLI processes in parallel or
overlap agents accessing the same organization. Package pacing coordinates
clients within a process; separate processes must be serialized by the caller.
None of these prove numerical or visual equivalence. Do not
execute business SQL or scripts unless separately requested.

Read per-resource `capture`, `editable`, `restrictions`, `dataset_bindings`, IDs
and locations in the import manifest. `capture=complete` and `editable=false`
means the readable source was preserved but faithful writing is unavailable.
Report this separately from inaccessible source or unverified revisions.
Do not turn off `read_only`, drop source references, or normalize the source to
make a write succeed. Unsupported resources must not prevent unrelated source
capture. Preserve unused attached datasets and repeated chart placements.

The SDK 3.1/3.2 public selector builder does not support selector pinning; the
native readable dashboard source remains capturable, while its writer may be
blocked. Authored HTML source is not exposed by those SDK versions. Dynamic
Editor dependency declarations and malformed Meta cannot establish closure; retain their
tabs and report the incomplete dependency closure. These are SDK/capture limits,
not a reason to read toolkit scripts or write project-specific Python.

For later edits use `plan` and scoped `apply` through the generated launcher.
Published verification can pass with a newer saved draft; this does not authorize
overwriting that draft. If pull rejects changed Editor tabs or unsupported Wizard
fidelity, keep the previous files and baseline. Import into another empty
destination to capture the changed source and inspect its current restrictions
before merging; do not weaken fidelity checks to make pull succeed.
Imported resources retain their individual folder/workbook locations; a source
dashboard's folder does not relocate dependencies. Moving or copying cloud
resources is a separate explicitly requested workflow. Existing recipes use
their project `import_dashboard`/`import_tab` merge methods; this new-project
command deliberately refuses a nonempty destination.

The matching public API is:

```python
from analytics_toolkit.datalens_utils import DataLensAuthConfig, import_dashboard

report = import_dashboard(
    source_url_or_id, destination,
    auth=DataLensAuthConfig(organization_id=org, yc_profile=profile, yc_binary=yc),
    runtime_root=runtime, dry_run=True, resume=True,
)
```

Use the CLI for ordinary consumer work. A library defect should be reported with
the failed stage, resource and request ID; fix it in the toolkit before repeating
the import with a fresh consumer. Do not repair the clone with custom orchestration.
