[Module index](index.md)

# Capture an existing published dashboard

Use the public [import_dashboard and DataLensAuthConfig](functions/import-dashboard.md)
entry point to create a standard local project from a URL or ID. The package CLI
is designed for ordinary imports without project-specific Python:

```sh
python -m analytics_toolkit.cli datalens import-dashboard 'SOURCE_URL_OR_ID' \
  --destination ./project --runtime-dir ./runtime \
  --organization-id EXISTING_ORG --yc-profile EXISTING_PROFILE --json
```

The command previews by default. Review the report, then repeat with
`--resume --write` to publish the local directory. Its destination must be absent
or empty; use the project's existing import methods for a collision-checked
merge. No import creates, moves or edits cloud objects. Source SQL and Editor
code are preserved, never executed.

All tabs, including hidden ones, are captured. A URL's tab query is ignored;
repeat `--tab ID` for an explicit subset. Subsets preserve the full dashboard
source but block its writer because only selected dependencies were captured.
Reuse an existing environment, YC profile and executable (`--yc-binary`), or
Enterprise base URL and token variable name (`--installation enterprise
--base-url URL --token-env VARIABLE`). Credentials remain external.

## Capture and editing support

The generated project contains its launcher, object/UI configuration, original
source, extracted SQL/Editor tabs, coverage and `configs/import.json`. Each
resource retains its ID and folder/workbook location. Dependencies need not
share the dashboard's folder. Moving or copying them is a separate operation.

The manifest reports `capture` and `editable` independently. Readable source may
be complete even when its writer is blocked. Unavailable resources and unknown
dynamic dependencies make capture incomplete. Unsupported readable features
remain preserved and cannot be silently discarded by a write. Wizard binding
analysis records declared datasets separately from fields' dataset/GUID owners;
unused attachments are retained. Connections are safe references, not credential
backups. SQL/JS assets preserve exact authored text independently of editable
recipe assets.

SDK 3.1/3.2 do not expose typed selector pinning or authored HTML retrieval.
Selector source can be preserved while dashboard writing is blocked; HTML
source cannot be claimed as captured. General multi-dataset chart writing also
depends on public adapter support. These limitations do not permit private HTTP
or payload workarounds.

## Consistency and recovery

The importer inventories each discovered dependency set, reads the published
branch, checks returned revisions, and compares a final inventory. Newer saved
drafts do not block published capture. Exact-revision caches are private and
can be reused with `--resume` after inventories are rechecked. Drift aborts
publication; rerun to refresh changed resources. Shared organization pacing
applies to individual SDK calls. Local output is staged and validated before
the new directory is published by rename.

Existing-project `import_dashboard` and `import_tab` use this same capture and
revision verification. They automatically reuse exact-revision caches and keep
their strict collision/copy contracts. Their source archive lives under
`configs/imports/<capture-id>/`, with a manifest describing captured resources,
dependencies, assets and fidelity. The archive records provenance without
changing the destination dashboard identity.

Existing-project merges and pulls compare destination fingerprints before writing
and journal both file versions under the external runtime's `local-transactions/`.
After a crash, the next project operation recovers before parsing configuration.
Committed transactions remain committed; incomplete transactions roll back.
Intervening edits produce a recovery conflict and preserve both files and journal.
Keep the same runtime, inspect the reported file and resolve the conflict before
retrying; deleting the journal bypasses recovery and can leave a partial recipe.

Progress goes to stderr. JSON stdout includes object counts, request count,
cache hits, capture issues, restrictions and `written`. Exit codes are 0 for
complete capture, 2 for a partial capture report/project, and 1 for failure.
Failures retain `import-failure.json` and completed caches in the runtime.
Stop exhausted SDK errors and retain request IDs; do not add retry loops.

Run `python dashboard.py validate --json` for offline integrity,
`status --json` for current metadata, and `verify --full --json` for published
content. These checks do not establish numerical or rendering equivalence.
Use `plan` before scoped writes; saved-draft and revision protections still apply.
Published-source verification accepts a newer saved draft, while writes still
require explicit draft reconciliation. Malformed Editor Meta is preserved with
an incomplete dependency-closure report rather than aborting unrelated capture.

If pull reports changed Editor tabs or unsupported Wizard fidelity, the previous
local files and baseline remain intact. Import into another empty destination to
capture the changed source and reassess writer restrictions before merging.

[Module index](index.md)
