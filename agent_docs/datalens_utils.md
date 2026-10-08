# DataLens utilities

Read analytics_toolkit/datalens_utils/README.md for the facade and recipe contract.
Import analytics_toolkit.datalens_utils explicitly; it has no atk alias.
Base imports support Python 3.8 without the optional SDK. Operations require
Python 3.10+ and datalens-sdk==3.1.0 via the datalens or all extra.

Keep deployment and recipe paths explicit, runtime state outside the recipe,
and SDK imports lazy. Use injected clients and offline MockTransport fixtures;
never access YC, cloud dashboards, credentials or data for unit validation.
Preserve typed SDK errors, atomic checkpoints, ID recovery and revision checks.
Batch inventory reads using the public SDK and pace requests by organization.
Dashboard consumers assume analytics-toolkit[all] is already installed and use
their existing interpreter; no project environment creation or bootstrap.
Run focused checks for datalens_utils, then the normal precommit workflow.
