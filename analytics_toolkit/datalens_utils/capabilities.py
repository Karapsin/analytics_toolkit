"""Operation-level SDK boundaries; inventories come from the public client."""

from __future__ import annotations

import sys
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from .errors import DataLensCapabilityError, DataLensConfigurationError

TESTED_SDK_VERSIONS = ("3.1.0", "3.2.0")
MATRIX_VERSION = 2

# These describe the SDK contract, separately from recipe reconciliation.
_LIMITS = {
    "dataset.join.update": "Reject ambiguous column inference and topology requiring new avatars.",
    "dataset.rls.verify": (
        "Persisted metadata only; no subject or query evaluation. QL bypasses dataset RLS."
    ),
    "dataset.cache.verify": "Persisted metadata only; invalidation queries are not executed.",
    "wizard.hierarchy.update": (
        "Stable declarations and members required; only typed supported edits."
    ),
    "wizard.aggregated_measure.update": (
        "Stable declarations required; no unsupported topology edits."
    ),
    "map.update": "Layer topology must remain unchanged.",
    "html_page.verify": (
        "Entry metadata and revision relationships only; getters return no authored source."
    ),
}
_BLOCKED = {
    "report": "Typed lifecycle, revision model, and metadata verification methods.",
    "pdf": "Documented export operation, job/result contract, and result retrieval.",
    "mailing": "Typed schedule, recipients, lifecycle, and status methods.",
    "private_embedding": "Typed embed/key lifecycle and documented token contract.",
    "wizard.multi_dataset": (
        "Dataset registration, links, and exact field resolution for general chart builders."
    ),
    "dataset.avatar": "Explicit source/avatar handling in the dataset update builder.",
    "html_page.source": "Documented authored-source retrieval contract.",
}
_FEATURES = {
    "connection": "connections",
    "collection": "collections",
    "workbook": "workbooks",
    "dataset.source": "datasets",
    "dataset.join": "datasets",
    "dataset.rls": "datasets",
    "dataset.cache": "datasets",
    "dashboard.selector": "dashboards",
    "dashboard.cross_filter": "dashboards",
    "dashboard.settings": "dashboards",
    "wizard.hierarchy": "datasets",
    "wizard.aggregated_measure": "datasets",
    "map": "datasets",
    "html_page": "html_pages",
}


# Attribute paths reference public root exports and public client namespaces.
_CONTRACTS = {
    "connection": ("ConnectionCreate", "build", "ConnectionUpdate", "set", "connection"),
    "collection": ("CollectionCreate", "build", "CollectionUpdate", "execute", "collection"),
    "workbook": ("WorkbookCreate", "build", "WorkbookUpdate", "execute", "workbook"),
    "dataset.source": ("DatasetCreate", "sources", "DatasetUpdate", "update_source", "dataset"),
    "dataset.join": (
        "DatasetCreate",
        "add_relation",
        "DatasetUpdate",
        "update_relation",
        "dataset",
    ),
    "dataset.rls": ("DatasetCreate", "add_rls", "DatasetUpdate", "update_rls", "dataset"),
    "dataset.cache": (
        "DatasetCreate",
        "update_cache_invalidation_source",
        "DatasetUpdate",
        "update_cache_invalidation_source",
        "dataset",
    ),
    "dashboard.selector": (
        "DashboardTab",
        "add_group_selector",
        "DashboardUpdate",
        "update_selector",
        "dashboard",
    ),
    "dashboard.cross_filter": (
        "DashboardTab",
        "add_chart",
        "DashboardUpdate",
        "replace_chart",
        "dashboard",
    ),
    "dashboard.settings": (
        "DashboardCreate",
        "settings",
        "DashboardUpdate",
        "settings",
        "dashboard",
    ),
    "wizard.hierarchy": (
        "WizardChartUpdate",
        "add_hierarchy",
        "WizardChartUpdate",
        "add_hierarchy",
        "wizard_chart",
    ),
    "wizard.aggregated_measure": (
        "WizardChartUpdate",
        "add_aggregated_measure",
        "WizardChartUpdate",
        "change_aggregation",
        "wizard_chart",
    ),
    "map": (
        "WizardChartCreate",
        "add_layer",
        "WizardChartUpdate",
        "geopoints_config",
        "wizard_chart",
    ),
    "html_page": ("HtmlPageCreate", "content", "HtmlPageUpdate", "content", "html_page"),
}


def _sdk_operation_support(sdk: Any, client: Any, feature: str, operation: str) -> bool:
    create_class, create_method, update_class, update_method, getter = _CONTRACTS[feature]
    if operation == "update":
        return hasattr(getattr(sdk, update_class, None), update_method)
    if operation != "create":
        return callable(getattr(client.get, getter, None))
    if feature == "connection":
        connectors = client.capabilities.get("connectors", {})
        return bool(connectors) and all(
            callable(getattr(client.create.connection, name, None)) for name in connectors
        )
    if feature == "map":
        return "geolayer" in client.capabilities.get("chart_factories", {}).get("wizard", ())
    return hasattr(getattr(sdk, create_class, None), create_method)


def _sdk_inventory(installation: str, *, ready: bool) -> tuple[dict[str, Any], dict[str, bool]]:
    if not ready:
        return {}, {}
    import datalens_sdk  # noqa: PLC0415 - Preserve SDK-free imports.
    from datalens_sdk import (  # noqa: PLC0415 - Preserve SDK-free imports.
        DataLensClientEnterprise,
        DataLensClientYC,
        NoAuthProvider,
    )

    factory = DataLensClientYC if installation == "yc" else DataLensClientEnterprise
    kwargs = {} if installation == "yc" else {"base_url": "https://capabilities.invalid"}
    with factory(auth=NoAuthProvider(), **kwargs) as client:
        inventories = client.capabilities
        support = {
            feature + "." + operation: _sdk_operation_support(
                datalens_sdk, client, feature, operation
            )
            for feature in _CONTRACTS
            for operation in ("create", "read", "update", "pull", "verify")
        }
    return inventories, support


def _operation_matrix(
    inventories: dict[str, Any], sdk_support: dict[str, bool], *, ready: bool
) -> dict[str, Any]:
    operations = {}
    for feature, namespace in _FEATURES.items():
        for operation in ("create", "read", "update", "pull", "verify"):
            key = feature + "." + operation
            available = (
                ready
                and namespace in inventories.get("namespaces", ())
                and sdk_support.get(key, False)
            )
            operations[key] = {
                "status": "available" if available else "unavailable",
                "sdk_status": "available" if available else "unavailable",
                "adapter_status": "restricted" if key in _LIMITS else "supported",
                "restrictions": [_LIMITS[key]] if key in _LIMITS else [],
                "reason": _LIMITS.get(key, "Public typed SDK operation.")
                if available
                else "A tested SDK on Python 3.10+ and this installation namespace are required.",
                "requirements": ["Public typed methods only", "SDK 3.1.0 or 3.2.0"],
            }
    for feature, requirement in _BLOCKED.items():
        for operation in ("create", "read", "update", "pull", "verify"):
            operations[feature + "." + operation] = {
                "status": "blocked",
                "sdk_status": "unavailable",
                "adapter_status": "unsupported",
                "restrictions": [],
                "reason": "No suitable public typed method in SDK 3.1/3.2.",
                "requirements": [requirement],
            }
    operations["html_page.pull"] = dict(operations["html_page.source.pull"])
    for key, reason, sdk_available in (
        (
            "wizard.parameters.update",
            "Wizard has no typed chart-level parameter setter; use dashboard placement parameters.",
            False,
        ),
        (
            "collection.adopt_root",
            "Root container enumeration is unavailable; supply a retained ID.",
            False,
        ),
        (
            "workbook.adopt_root",
            "Root container enumeration is unavailable; supply a retained ID.",
            False,
        ),
        ("html_page.import", "Authored HTML cannot be retrieved by the public getter.", False),
        (
            "dashboard.layout.fractional",
            (
                "SDK 3.1/3.2 public Position and layout builders require whole "
                "grid coordinates; fractional authoring needs a typed SDK layout "
                "contract."
            ),
            False,
        ),
    ):
        operations[key] = {
            "status": "blocked",
            "sdk_status": "available" if sdk_available else "unavailable",
            "adapter_status": "unsupported",
            "reason": reason,
            "restrictions": [reason],
            "requirements": [reason],
        }
    for operation in ("dashboard.import", "dashboard.tab.import"):
        operations[operation] = {
            "status": "available" if ready else "unavailable",
            "sdk_status": "available" if ready else "unavailable",
            "adapter_status": "restricted",
            "reason": "Public metadata import with a fidelity report.",
            "restrictions": [
                "Unresolved dynamic dependencies and unrepresentable settings block local writes."
            ],
            "requirements": [
                "Version-2 recipe or explicit upgrade",
                "Compatible resource locations",
            ],
        }
    return operations


def get_capabilities(*, installation: str = "yc") -> dict[str, Any]:
    """Return the tested SDK operation matrix without authentication or requests."""
    if installation not in {"yc", "enterprise"}:
        msg = "installation must be yc or enterprise."
        raise DataLensConfigurationError(msg)
    try:
        installed = version("datalens-sdk")
    except PackageNotFoundError:
        installed = None
    ready = installed in TESTED_SDK_VERSIONS and sys.version_info >= (3, 10)
    inventories, sdk_support = _sdk_inventory(installation, ready=ready)
    operations = _operation_matrix(inventories, sdk_support, ready=ready)
    return {
        "matrix_version": MATRIX_VERSION,
        "sdk_version": installed,
        "tested_sdk_versions": list(TESTED_SDK_VERSIONS),
        "installation": installation,
        "operations": operations,
        "inventories": {
            "connectors": sorted(inventories.get("connectors", {})),
            "dataset_sources": sorted(inventories.get("dataset_sources", {})),
            "chart_factories": inventories.get("chart_factories", {}),
        },
    }


def require_capability(report: dict[str, Any], operation: str) -> None:
    entry = report["operations"].get(operation)
    if entry is None or entry["status"] != "available":
        raise DataLensCapabilityError(operation, report)
