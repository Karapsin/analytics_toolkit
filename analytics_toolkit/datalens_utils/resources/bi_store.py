"""Version-2 checkpoints for workbook and folder resources, with owned writes."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from datalens_sdk import EntryLocation

from analytics_toolkit.datalens_utils.deployment import BIProjectDeployment, TargetLocation
from analytics_toolkit.datalens_utils.editing.state import fingerprint
from analytics_toolkit.datalens_utils.errors import DataLensUtilsError
from analytics_toolkit.datalens_utils.session import current_session

from .store import ResourceStore

CHECKPOINT_VERSION = 2

CONNECTION_METADATA_FIELDS = frozenset(
    {
        "host",
        "port",
        "db_name",
        "schema",
        "schema_name",
        "username",
        "user_name",
        "secure",
        "ssl_ca_verify",
        "readonly",
        "raw_sql_level",
        "data_export_forbidden",
        "cache_ttl_sec",
        "cache_invalidation_throttling_interval_sec",
        "listing_sources",
        "project_id",
        "cloud_id",
        "folder_id",
        "service_account_id",
        "account_name",
        "warehouse",
        "counter_id",
        "accuracy",
        "allowed_methods",
        "db_connect_method",
        "alias",
        "auth_type",
        "client_id",
        "connection_manager_cloud_id",
        "connection_manager_connection_id",
        "connection_manager_delegation_is_set",
        "connection_manager_folder_id",
        "delegation_is_set",
        "enforce_collate",
        "experimental_features",
        "mdb_cluster_id",
        "mdb_folder_id",
        "path",
        "portal",
        "refresh_token_expire_time",
        "ssl_ca",
        "ssl_enable",
        "url",
        "user_role",
    }
)


def safe_parameter(key: str, value: Any) -> Any:
    if key not in {"url", "portal"} or not isinstance(value, str):
        return value
    parsed = urlsplit(value)
    host = parsed.netloc.rsplit("@", 1)[-1]
    parameters = parse_qsl(parsed.query, keep_blank_values=True)
    query = [
        (name, item)
        for name, item in parameters
        if not any(
            secret in name.lower()
            for secret in ("token", "secret", "password", "key", "credential")
        )
    ]
    return urlunsplit(
        (
            parsed.scheme,
            host,
            parsed.path,
            parsed.query if len(query) == len(parameters) else urlencode(query),
            "",
        )
    )


def entry_description(entity: Any) -> Any:
    annotation = getattr(entity, "raw", {}).get("annotation") or {}
    return getattr(entity, "description", annotation.get("description", ""))


def safe_metadata(entity: Any, kind: str) -> dict[str, Any]:
    result = {
        "id": entity.id,
        "name": entity.name,
        "description": entry_description(entity),
    }
    if kind in {"collection", "workbook"}:
        result["parent_id"] = getattr(entity, "parent_id", getattr(entity, "collection_id", None))
    else:
        result.update(
            path=getattr(entity, "key", None), workbook_id=getattr(entity, "workbook_id", None)
        )
        if kind == "connection":
            result.update(
                connector=entity.type,
                parameters={
                    key: safe_parameter(key, value)
                    for key, value in entity.raw.items()
                    if key in CONNECTION_METADATA_FIELDS
                },
            )
        else:
            result.update(saved_id=entity.saved_id, published_id=entity.published_id)
            if kind == "html_page":
                result.update(
                    object_id=entity.object_id,
                    version=entity.version,
                    policy_version=entity.policy_version,
                )
    return result


def deployment_identity() -> dict[str, Any]:
    deployment = current_session().deployment
    target = (
        deployment.target
        if isinstance(deployment, BIProjectDeployment)
        else TargetLocation.path(deployment.target_path)
    )
    return {
        "installation": getattr(deployment, "installation", "yc"),
        "organization_id": deployment.organization_id,
        "base_url": getattr(deployment, "base_url", None),
        "target": {"kind": target.kind, "value": target.value, "reference": target.reference},
        "dashboard_name": deployment.dashboard_name,
    }


class BIResourceStore(ResourceStore):
    """Shared resource operations with explicit location and checkpoint ownership."""

    def __init__(self, registry: Any, *, readonly: bool = False) -> None:
        self.registry = registry
        self.readonly = readonly
        self.path = current_session().paths.runtime_root / "resources.json"
        self.desired_target = deployment_identity()
        self.mutation_recorder = None
        self.pending_metadata: dict[str, Any] = {}
        self.state = (
            json.loads(self.path.read_text(encoding="utf-8"))
            if self.path.exists()
            else {"version": 2, "target": self.desired_target, "resources": {}}
        )
        if self.state.get("version") == 1:
            previous = self.state.get("target", {})
            desired = self.desired_target
            default = registry.resources.get("connection:default")
            if (
                desired["target"]["kind"] != "path"
                or desired["installation"] != "yc"
                or previous.get("organization_id") != desired["organization_id"]
                or previous.get("folder_path", "").strip("/")
                != desired["target"]["value"].strip("/")
                or default is None
                or previous.get("connection_id") != default.definition.get("id")
            ):
                msg = (
                    "Version-1 checkpoint identity does not match this deployment; "
                    "migration refused."
                )
                raise DataLensUtilsError(msg)
            self.state = {**self.state, "version": 2, "target": desired, "legacy_target": previous}
            edit_path = current_session().paths.runtime_root / "edit-state.json"
            if edit_path.is_file():
                self.state["legacy_edit_state"] = json.loads(edit_path.read_text(encoding="utf-8"))
        if (
            self.state.get("version") != CHECKPOINT_VERSION
            or self.state.get("target") != self.desired_target
        ):
            msg = (
                "Checkpoint identity changed; folder/workbook boundary migrations "
                "require a separate deployment."
            )
            raise DataLensUtilsError(msg)
        self.folders: dict[str, Any] = {}
        self.entries: dict[str, Any] = {}
        for key, resource in registry.resources.items():
            internal = self.internal_key(key)
            old = self.state["resources"].get(internal, {})
            configured = resource.definition.get("id")
            if configured and old.get("id") and old["id"] != configured:
                msg = f"Configured ID and checkpoint disagree for {key}."
                raise DataLensUtilsError(msg)
            if configured and not old:
                self.state["resources"][internal] = {
                    "id": configured,
                    "name": resource.definition.get("name"),
                    "scope": self.scope(resource.kind),
                }

    @staticmethod
    def internal_key(key: str) -> str:
        return "dashboard" if key == "dashboard:main" else key

    @staticmethod
    def scope(kind: str) -> str:
        return {"dashboard": "dash", "chart": "widget"}.get(kind, kind)

    def _save(self) -> None:
        if self.readonly:
            msg = "Read-only resource planning cannot write checkpoints."
            raise DataLensUtilsError(msg)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        super()._save()

    def save_checkpoint(self) -> None:
        """Persist the store without exposing ResourceStore implementation methods."""
        self._save()

    def set_location(self, location: Any) -> None:
        self.folder = location
        self.entry_location = (
            EntryLocation.workbook(location.id)
            if hasattr(location, "collection_id") and not hasattr(location, "key")
            else EntryLocation.path(location.key.rstrip("/"))
        )
        self.folders = dict.fromkeys(
            ("dash", "widget", "dataset", "connection", "html_page"), location
        )
        entries = list(location.list_entries())
        self.entries = dict.fromkeys(self.folders, entries)

    def verify_location(self, entity: Any, name: Any = None, *, scope: Any = None) -> None:
        location = self.folder_for(scope or "dash")
        if hasattr(location, "collection_id") and not hasattr(location, "key"):
            valid = getattr(entity, "workbook_id", None) == location.id
        else:
            valid = getattr(entity, "workbook_id", None) is None and (
                getattr(entity, "key", None) or ""
            ).rstrip("/").rpartition("/")[0] == location.key.rstrip("/")
        if not valid:
            msg = (
                f"Resource {entity.id} is outside the configured folder/workbook; "
                "migration refused."
            )
            raise DataLensUtilsError(msg)
        if name is not None and entity.name != name:
            msg = f"Resource {entity.id} has an unexpected name."
            raise DataLensUtilsError(msg)

    @staticmethod
    def verify_identity(entity: Any, identifier: str) -> None:
        if entity.id != identifier:
            message = "Returned resource identity differs from the requested ID."
            raise DataLensUtilsError(message)

    def existing(
        self, key: Any, name: Any, getter: Any, *, scope: Any, refresh: Any = False
    ) -> Any:
        entry = self.state["resources"].get(key)
        if entry:
            if entry.get("scope") != scope:
                msg = f"Resource kind changed for {key}."
                raise DataLensUtilsError(msg)
            kind = "dashboard" if key == "dashboard" else key.split(":", 1)[0]
            branch = "saved" if entry.get("pending_write") else entry.get("branch", "published")
            identifier = entry["id"]
            entity = getter(
                by_id=entry["id"],
                **(
                    {"branch": branch}
                    if kind in {"chart", "dataset", "dashboard", "html_page"}
                    else {}
                ),
            )
        else:
            entries = (
                list(self.folder_for(scope).list_entries()) if refresh else self.entries[scope]
            )
            matches = [
                item
                for item in entries
                if item.name == name
                and (
                    item.scope == scope
                    or (
                        scope == "html_page"
                        and item.scope == "artifact"
                        and getattr(item, "type", None) == "html-page"
                    )
                )
            ]
            if len(matches) > 1:
                msg = f"Multiple {scope} resources named {name!r}."
                raise DataLensUtilsError(msg)
            if not matches:
                return None
            identifier = matches[0].id
            entity = getter(by_id=identifier)
        self.verify_identity(entity, identifier)
        self.verify_location(entity, scope=scope)
        self.check_write("dashboard:main" if key == "dashboard" else key, entity)
        self.checkpoint(key, entity, scope=scope)
        return entity

    def checkpoint(
        self, key: Any, entity: Any, *, scope: Any = None, mutation: Any = False
    ) -> None:
        entry = self.state["resources"].setdefault(key, {})
        kind = "dashboard" if key == "dashboard" else key.split(":", 1)[0]
        entry.update(id=entity.id, name=entity.name)
        if scope is not None:
            entry["scope"] = scope
        if mutation:
            entry.update(self.pending_metadata.pop(key, {}))
            resource = self.registry.resources["dashboard:main" if key == "dashboard" else key]
            entry["pending_write"] = {
                "fingerprint": fingerprint(resource.files),
                "metadata": safe_metadata(entity, kind),
            }
        self._save()

    def remember(self, key: str, entity: Any) -> None:
        resource = self.registry.resources[key]
        entry = self.state["resources"][self.internal_key(key)]
        entry.update(
            baseline=resource.files,
            fingerprint=fingerprint(resource.files),
            metadata=safe_metadata(entity, resource.kind),
            branch="published",
        )
        entry.pop("pending_write", None)
        self._save()

    def check_write(self, key: str, entity: Any) -> None:
        resource = self.registry.resources[key]
        old = self.state["resources"].get(self.internal_key(key), {})
        live = safe_metadata(entity, resource.kind)
        pending = old.get("pending_write", {})
        if (
            pending.get("fingerprint") == fingerprint(resource.files)
            and pending.get("metadata") == live
        ):
            return
        if old.get("metadata") and old["metadata"] != live:
            raise DataLensUtilsError(key + " changed remotely; pull before applying.")
        if (
            resource.kind in {"dataset", "chart", "dashboard", "html_page"}
            and entity.saved_id != entity.published_id
            and not (old.get("branch") == "saved" and old.get("metadata") == live)
        ):
            raise DataLensUtilsError(key + " has an unimported remote draft.")

    def persisted(self, key: Any, entity: Any, getter: Any, *, branch: Any = "published") -> Any:
        self.checkpoint(key, entity, mutation=True)
        kind = "dashboard" if key == "dashboard" else key.split(":", 1)[0]
        identifier = entity.id
        entity = getter(
            by_id=identifier,
            **(
                {"branch": branch} if kind in {"dataset", "chart", "dashboard", "html_page"} else {}
            ),
        )
        self.verify_identity(entity, identifier)
        if kind not in {"collection", "workbook"}:
            self.verify_location(entity)
        return entity
