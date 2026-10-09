# Editing and recovery

Preserve semantic keys, configured IDs, external `resources.json` and
`edit-state.json`. IDs seed fresh state and must agree with checkpoints. A
separate deployment needs a separate runtime and deliberate ID reset. Do not
delete older dashboards or unused objects during reconciliation.

```sh
python dashboard.py status
python dashboard.py apply --chart wizard_revenue
python dashboard.py apply --dataset sales
python dashboard.py pull --ui
python dashboard.py pull --chart wizard_revenue --branch saved
python dashboard.py verify --full
```

`pull` uses a three-way merge against the verified baseline. Published is the
default; importing drafts requires `--branch saved`. `apply` protects remote
changes, locks and unimported drafts and updates dataset consumers. Full
reconciliation handles creation, identity recovery and folder moves.

A successful create/update/move is persisted even when its later fetch fails.
Checkpoint immediately, then re-fetch and verify; do not blindly create again.
Adopt exact-name conflicts only within the expected folder, rejecting
ambiguity. Typed not-found means absence; auth/forbidden/network/429 do not.

Some field/item changes require draft removal, re-fetch, restoration, validation
and publication. Keep phases durable. Geolayer topology and some combined/
family changes are create-only in SDK 3.1.0; reject unsupported topology changes
before writing. Preserve unrelated dashboard items.

Folder moves retain IDs within the recorded source boundary. Commit a target
after verification/export. Export freshly fetched revisions externally,
including every chart/dataset revision and path; dashboard revision alone is
insufficient. Avoid concurrent UI editing: resources are not one atomic snapshot.

For named BI resources and tab/whole-dashboard imports, see [BI projects](bi-projects.md).
