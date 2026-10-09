"""Read-only resource planning, including dependent and prerequisite effects."""

from __future__ import annotations

import json
from typing import Any, Sequence

from .auth.client import datalens_client
from .capabilities import get_capabilities
from .editing.state import configuration, fingerprint, metadata
from .errors import DataLensConfigurationError
from .recipe import load_registry
from .session import current_session

BI_RECIPE_VERSION = 2


def legacy_action(resource: Any, entry: Any, old: Any, local: str, identifier: Any) -> Any:
    key = resource.key
    revisioned = {"dataset", "chart", "dashboard", "html_page"}
    conflicts: list[dict[str, Any]] = []
    action = "create_or_adopt" if not identifier else "verify" if old is None else "unchanged"
    if identifier and entry is None:
        action = "missing"
        conflicts.append(
            {
                "resource": key,
                "reason": (
                    "Retained resource is missing; recreation requires deliberate ID reset."
                ),
            }
        )
    if entry is not None and old is not None:
        live = metadata(entry) if resource.kind in revisioned else {"id": entry.id}
        remote_changed = any(live.get(name) != old.get(name) for name in live)
        local_changed = local != old.get("fingerprint")
        action = (
            "conflict"
            if remote_changed and local_changed
            else "pull"
            if remote_changed
            else "update"
            if local_changed
            else "unchanged"
        )
        if resource.kind in revisioned and entry.saved_id != entry.published_id:
            action = "conflict"
            conflicts.append(
                {
                    "resource": key,
                    "reason": "Remote saved draft requires explicit reconciliation.",
                }
            )
        if remote_changed and local_changed:
            conflicts.append(
                {
                    "resource": key,
                    "reason": "Local and remote metadata changed since the baseline.",
                }
            )
    if resource.kind == "connection" and resource.definition.get("mode") == "existing":
        action = "reference"
    return action, conflicts


def plan(*, resource_keys: Sequence[str] | None = None) -> dict[str, Any]:
    state = current_session()
    registry = load_registry()
    if state.runtime.get("schema_version", 1) == BI_RECIPE_VERSION:
        return plan_v2(registry, resource_keys=resource_keys)
    selected = registry.selection(resource_keys)
    dependencies, affected = registry.closure(selected)
    path = state.paths.runtime_root / "edit-state.json"
    previous = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    checkpoint_path = state.paths.runtime_root / "resources.json"
    checkpoint = (
        json.loads(checkpoint_path.read_text(encoding="utf-8")) if checkpoint_path.exists() else {}
    )
    legacy = state.runtime.get("schema_version", 1) == 1
    units = configuration() if legacy else {}
    capabilities = get_capabilities(installation=state.runtime["installation"])
    actions, conflicts, blockers = [], [], []
    inspected = list(dict.fromkeys([*dependencies, *affected]))
    ids = {}
    for key in inspected:
        resource = registry.resources[key]
        old_key = "dashboard" if legacy and key == "dashboard:main" else key
        configured = resource.definition.get("id")
        retained = checkpoint.get("resources", {}).get(old_key, {}).get("id")
        if configured and retained and configured != retained:
            conflicts.append(
                {"resource": key, "reason": "Configured ID differs from checkpoint ownership."}
            )
        ids[key] = configured or retained
    revisioned = {"dataset", "chart", "dashboard", "html_page"}
    entry_ids = [
        identifier
        for key, identifier in ids.items()
        if identifier and registry.resources[key].kind in revisioned
    ]
    if len(entry_ids) != len(set(entry_ids)):
        msg = "Configured resource IDs must be unique."
        raise DataLensConfigurationError(msg)
    with datalens_client() as client:
        entries = (
            {
                entry.id: entry
                for entry in client.navigation.get_entries(ids=entry_ids, page_size=100)
            }
            if entry_ids
            else {}
        )
        for key in inspected:
            resource = registry.resources[key]
            old_key = "dashboard" if legacy and key == "dashboard:main" else key
            old = previous.get("resources", {}).get(old_key)
            unit = units.get(old_key)
            local = unit["fingerprint"] if unit else fingerprint(resource.files)
            entry = entries.get(ids[key])
            if resource.kind not in revisioned and ids[key]:
                entry = getattr(client.get, resource.kind)(by_id=ids[key])
            action, resource_conflicts = legacy_action(resource, entry, old, local, ids[key])
            conflicts.extend(resource_conflicts)
            if resource.kind == "html_page" and action in {"pull", "conflict"}:
                blockers.append(
                    {
                        "resource": key,
                        "operation": "html_page.pull",
                        **capabilities["operations"]["html_page.pull"],
                    }
                )
            actions.append(
                {
                    "resource": key,
                    "action": action,
                    "selected": key in selected,
                    "id": ids[key],
                    "dependencies": list(resource.dependencies),
                }
            )
    return {
        "actions": actions,
        "selected": sorted(selected),
        "dependencies": [key for key in dependencies if key not in selected],
        "affected_dependents": affected,
        "conflicts": conflicts,
        "capability_blockers": blockers,
        "orphans": sorted(
            set(checkpoint.get("resources", {}))
            - {
                "dashboard" if legacy and key == "dashboard:main" else key
                for key in registry.resources
            }
        ),
        "capabilities": capabilities,
    }


def classify(local: str, old: dict[str, Any], live: dict[str, Any] | None) -> str:
    """Compare the same resource checkpoint used by reconciliation."""
    if live is None:
        return "missing" if old.get("id") else "create_or_adopt"
    pending = old.get("pending_write", {})
    if pending.get("fingerprint") == local and pending.get("metadata") == live:
        return "recover"
    if live.get("saved_id") != live.get("published_id") and not (
        old.get("branch") == "saved" and old.get("metadata") == live
    ):
        return "conflict"
    if "metadata" not in old or "fingerprint" not in old:
        return "verify"
    remote = live != old["metadata"]
    changed = local != old["fingerprint"]
    return (
        "conflict"
        if remote and changed
        else "pull"
        if remote
        else "update"
        if changed
        else "unchanged"
    )


def plan_v2(registry: Any, *, resource_keys: Sequence[str] | None) -> dict[str, Any]:
    from .bi_engine import read_entities  # noqa: PLC0415 - Lazy SDK boundary or module cycle.
    from .bi_preflight import (  # noqa: PLC0415 - Keep planning importable without SDK.
        operation_blockers,
    )
    from .bi_verification import resource_issues  # noqa: PLC0415 - Read-only SDK boundary.
    from .resources.bi_store import (  # noqa: PLC0415 - Lazy SDK boundary or module cycle.
        BIResourceStore,
        safe_metadata,
    )

    selected = registry.selection(resource_keys)
    inspected, writes, affected = registry.read_closure(selected)
    store = BIResourceStore(registry, readonly=True)
    capabilities = get_capabilities(installation=current_session().runtime["installation"])
    actions, conflicts = [], []
    with datalens_client() as client:
        entities = read_entities(client, store, registry, inspected, allow_missing=True)
        blockers = operation_blockers(client, store, registry, writes, entities)
        for key in inspected:
            resource = registry.resources[key]
            old = store.state["resources"].get(store.internal_key(key), {})
            live = safe_metadata(entities[key], resource.kind) if key in entities else None
            action = classify(fingerprint(resource.files), old, live)
            if (
                resource.kind == "dashboard"
                and action == "unchanged"
                and resource_issues(client, store, registry, key, entities)
            ):
                action = "update"
            if (
                resource.kind == "connection"
                and resource.definition["mode"] == "existing"
                and key in entities
                and resource_issues(client, store, registry, key, entities)
            ):
                action = "conflict"
            if action in {"missing", "conflict"}:
                conflicts.append(
                    {
                        "resource": key,
                        "reason": (
                            "Retained resource missing, remote draft, or concurrent local and "
                            "remote edits."
                        ),
                    }
                )
            if (
                resource.kind == "connection"
                and resource.definition["mode"] == "existing"
                and live is not None
                and action != "conflict"
            ):
                action = "reference"
            if resource.kind == "html_page" and action in {"pull", "conflict"}:
                blockers.append(
                    {
                        "resource": key,
                        "operation": "html_page.pull",
                        **capabilities["operations"]["html_page.pull"],
                    }
                )
            if key not in writes and action not in {"unchanged", "reference"}:
                conflicts.append(
                    {
                        "resource": key,
                        "reason": "Required dependency must be reconciled or included in scope.",
                    }
                )
            actions.append(
                {
                    "resource": key,
                    "action": action,
                    "selected": key in selected,
                    "will_write": key in writes
                    and action in {"update", "create_or_adopt", "recover"},
                    "id": old.get("id"),
                    "dependencies": list(resource.dependencies),
                }
            )
    return {
        "actions": actions,
        "selected": sorted(selected),
        "write_scope": sorted(writes),
        "dependencies": [key for key in inspected if key not in selected],
        "read_dependencies": [key for key in inspected if key not in writes],
        "affected_dependents": affected,
        "conflicts": conflicts,
        "capability_blockers": blockers,
        "orphans": sorted(
            set(store.state["resources"]) - {store.internal_key(key) for key in registry.resources}
        ),
        "capabilities": capabilities,
    }
