"""Published dependency capture with resumable exact-revision reads."""

from __future__ import annotations

import copy
import json
from importlib.metadata import version
from typing import TYPE_CHECKING, Any

from datalens_sdk import ForbiddenError, NotFoundError

from .bi_import import collect_dependencies
from .errors import DataLensConfigurationError, DataLensUtilsError
from .import_source import (
    CAPTURE_VERSION,
    atomic_json,
    digest,
    metadata,
    model_record,
    restore_model,
    source_assets,
    source_document,
)
from .session import current_session

if TYPE_CHECKING:
    from pathlib import Path


def inventory(client: Any, identifiers: set[str]) -> dict[str, dict[str, Any]]:
    if not identifiers:
        return {}
    return {
        entry.id: {
            name: getattr(entry, name, None)
            for name in (
                "id",
                "name",
                "key",
                "scope",
                "type",
                "workbook_id",
                "saved_id",
                "published_id",
            )
        }
        for entry in client.navigation.get_entries(ids=sorted(identifiers), page_size=100)
    }


def revision_identity(entry: dict[str, Any]) -> dict[str, Any]:
    names = ("id", "name", "key", "scope", "type", "workbook_id", "published_id")
    result = {name: entry.get(name) for name in names}
    if entry.get("scope") == "connection":
        result["saved_id"] = entry.get("saved_id")
    return result


class Capture:
    """One snapshot shared by conversion and source verification; never cloud writes."""

    def __init__(self, client: Any, *, resume: bool = False) -> None:
        self.client = client
        self.resume = resume
        self.before: dict[str, dict[str, Any]] = {}
        self.records: dict[str, dict[str, Any]] = {}
        self.entities: dict[str, Any] = {}
        self.kinds: dict[str, str] = {}
        self.issues: list[dict[str, Any]] = []
        self.cache_hits = 0
        self.verified = False
        self.stage = "Inventory"
        self.active_resource: str | None = None
        state = current_session()
        identity = {
            "installation": state.runtime["installation"],
            "organization_id": state.runtime.get("organization_id"),
            "base_url": getattr(state.deployment, "base_url", None),
            "sdk": version("datalens-sdk"),
            "capture_version": CAPTURE_VERSION,
        }
        self.root = state.paths.runtime_root / "imports" / digest(identity)
        self.root.mkdir(parents=True, exist_ok=True)

    def announce(self, stage: str) -> None:
        self.stage = stage
        if stage != "Capture":
            self.active_resource = None
        state = current_session()
        state.emit(
            f"{stage}: {len(self.records)}/{len(self.kinds)} objects; "
            f"requests={state.runtime.get('_request_count', 0)}; cache_hits={self.cache_hits}",
            flush=True,
        )

    def register(self, kind: str, identifiers: set[str]) -> dict[str, str]:
        self.stage = "Inventory"
        self.active_resource = None
        identifiers = identifiers - self.kinds.keys()
        for identifier in identifiers:
            self.kinds[identifier] = kind
        try:
            observed = inventory(self.client, identifiers)
        except (ForbiddenError, NotFoundError) as error:
            observed = {}
            self.issues.append(
                {
                    "feature": "revision inventory",
                    "reason": str(error),
                    "request_id": error.context.request_id,
                }
            )
        self.before.update(observed)
        scopes = {identifier: entry.get("scope", "") for identifier, entry in observed.items()}
        for identifier, scope in scopes.items():
            if kind == "chart" and scope == "artifact":
                self.kinds[identifier] = "html_page"
        return scopes

    def read(self, identifier: str) -> Any:
        self.stage = "Capture"
        self.active_resource = self.kinds[identifier] + ":" + identifier
        if identifier in self.entities:
            return self.entities[identifier]
        kind = self.kinds[identifier]
        expected = self.before.get(identifier, {})
        revision = expected.get("saved_id" if kind == "connection" else "published_id")
        cache = self.root / "cache" / digest({"kind": kind, "id": identifier, "revision": revision})
        if self.resume and revision and cache.is_file():
            entity = self.cached(identifier, cache, expected)
            if entity is not None:
                return entity
        try:
            entity = getattr(self.client.get, kind)(
                by_id=identifier, **({} if kind == "connection" else {"branch": "published"})
            )
        except (ForbiddenError, NotFoundError) as error:
            self.issues.append(
                {
                    "resource": kind + ":" + identifier,
                    "feature": "source",
                    "reason": str(error),
                    "request_id": error.context.request_id,
                }
            )
            return None
        if entity.id != identifier:
            raise DataLensUtilsError("Returned resource identity differs: " + identifier)
        if kind != "connection" and revision and entity.rev_id != revision:
            raise DataLensUtilsError("Published source changed during capture: " + identifier)
        if not expected or (kind != "connection" and not revision):
            self.issues.append(
                {
                    "resource": kind + ":" + identifier,
                    "feature": "revision",
                    "reason": "Published revision unavailable; consistency is unverified.",
                }
            )
        try:
            document = source_document(entity, kind)
            record = {
                "source": document,
                "assets": source_assets(document),
                "model": model_record(entity),
            }
        except DataLensUtilsError as error:
            self.issues.append(
                {"resource": kind + ":" + identifier, "feature": "source", "reason": str(error)}
            )
            self.entities[identifier] = entity
            return entity
        self.records[identifier] = record
        self.entities[identifier] = entity
        atomic_json(
            cache,
            {"identity": revision_identity(expected), "record": record, "digest": digest(record)},
        )
        self.announce("Capture")
        return entity

    def cached(self, identifier: str, path: Path, expected: dict[str, Any]) -> Any:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("identity") != revision_identity(expected) or payload.get(
                "digest"
            ) != digest(payload["record"]):
                return None
            entity = restore_model(payload["record"]["model"])
            if entity.id != identifier:
                return None
        except (ValueError, TypeError, KeyError, DataLensConfigurationError):
            current_session().emit("Ignoring invalid cached capture for " + identifier)
            return None
        self.records[identifier] = payload["record"]
        self.entities[identifier] = entity
        self.cache_hits += 1
        self.announce("Capture")
        return entity

    def discover(self, dashboard_id: str, tab_ids: Any = None) -> set[str]:
        self.register("dashboard", {dashboard_id})
        dashboard = self.read(dashboard_id)
        if dashboard is None:
            raise DataLensUtilsError("Source dashboard is inaccessible: " + dashboard_id)
        selected = set(tab_ids) if tab_ids is not None else {tab.id for tab in dashboard.tabs}
        if not selected or selected - {tab.id for tab in dashboard.tabs}:
            msg = "Requested tabs are empty or absent from the source dashboard."
            raise DataLensConfigurationError(msg)
        report: dict[str, Any] = {"blockers": []}
        collect_dependencies(
            dashboard,
            selected,
            report,
            self.register,
            lambda _kind, identifier: self.read(identifier),
        )
        self.issues.extend({**item, "feature": "dependency closure"} for item in report["blockers"])
        return selected

    def finish(self) -> None:
        self.announce("Verify revisions")
        try:
            after = inventory(self.client, set(self.kinds))
        except (ForbiddenError, NotFoundError) as error:
            self.issues.append(
                {
                    "feature": "revision inventory",
                    "reason": str(error),
                    "request_id": error.context.request_id,
                }
            )
            return
        drift = [
            identifier
            for identifier, entry in self.before.items()
            if revision_identity(entry) != revision_identity(after.get(identifier, {}))
        ]
        atomic_json(
            self.root / "inventory.json", {"before": self.before, "after": after, "drift": drift}
        )
        if drift:
            raise DataLensUtilsError(
                "Source inventory changed; resume to refresh affected captures: "
                + ", ".join(sorted(drift))
            )
        self.verified = set(after) == set(self.kinds) == set(self.before)

    def source_files(self, keys: dict[str, str]) -> tuple[dict[str, Any], dict[str, Any]]:
        files: dict[str, Any] = {}
        resources = {}
        for identifier, kind in self.kinds.items():
            record = self.records.get(identifier)
            entry = copy.deepcopy(self.before.get(identifier, {}))
            relative = "configs/source/" + keys[identifier].replace(":", "/") + ".json"
            details = {
                "id": identifier,
                "kind": kind,
                "metadata": entry,
                "capture": "complete" if record else "unavailable",
                "source_file": relative if record else None,
                "source_hash": digest(record["source"]) if record else None,
                "editable": False,
                "restrictions": [],
            }
            if record:
                files[relative] = record["source"]
                files.update(record["assets"])
                details["metadata"] = metadata(self.entities[identifier])
            resources[keys[identifier]] = details
        return files, resources
