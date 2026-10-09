# DataLens utilities

Read analytics_toolkit/datalens_utils/README.md for the facade and recipe contract.
Import analytics_toolkit.datalens_utils explicitly; it has no atk alias.
Base imports support Python 3.8 without the optional SDK. Operations require
Python 3.10+ and SDK 3.1.0 or 3.2.0; new datalens/all installs use 3.2.0.

Keep deployment and recipe paths explicit, runtime state outside the recipe,
and SDK imports lazy. Use injected clients and offline MockTransport fixtures;
never access YC, cloud dashboards, credentials or data for unit validation.
Preserve typed SDK errors, atomic checkpoints, ID recovery and revision checks.
Batch inventory reads using the public SDK and pace requests by organization.
Dashboard consumers assume analytics-toolkit[all] is already installed and use
their existing interpreter; no project environment creation or bootstrap.
Run focused checks for datalens_utils, then the normal precommit workflow.

Version-2 plan/status must use the reconciliation checkpoint and public resource
keys. Scoped writes load all prerequisites of affected dependents. Verify
managed recipe semantics as well as revisions; retain chart-group member routes.
Capabilities distinguish SDK availability from adapter restrictions. Tab and
whole-dashboard imports preview a collision-checked local merge with a fidelity
report. No cloud writes or dynamic script evaluation occur during import.
