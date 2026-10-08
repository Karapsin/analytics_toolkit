"""Exact-name adoption and durable identity independent of editable UI names."""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from datalens_sdk import ConflictError

from analytics_toolkit.datalens_utils.errors import DataLensUtilsError
from analytics_toolkit.datalens_utils.session import current_session as session


class ResourceStore:
    def __init__(  # noqa: PLR0913
        self,
        folder: Any,
        target: Any,
        *,
        path: Any = None,
        allow_folder_move: Any = False,
        entries: Any = None,
        dataset_ids: Any = None,
    ) -> None:
        self.folder = folder
        self.desired_target = target
        self.allow_folder_move = allow_folder_move
        self.mutation_recorder = None
        self.path = path if path is not None else session().paths.runtime_root / "resources.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.set_folders(dict.fromkeys(("dash", "widget", "dataset"), folder), entries=entries)
        self.state = (
            json.loads(self.path.read_text(encoding="utf-8"))
            if self.path.exists()
            else {
                "version": 1,
                "target": target,
                "resources": {},
            }
        )
        previous = self.state.get("target", {})
        previous_sources = previous.get("source_tables", {})
        desired_sources = target.get("source_tables", {})
        changed_roles = {
            role for role, table in previous_sources.items() if table != desired_sources.get(role)
        }
        same_sources = set(previous_sources.values()) == set(desired_sources.values())
        expanding_sources = previous_sources.items() <= desired_sources.items()
        owned_source_update = (
            previous_sources.keys() == desired_sources.keys()
            and bool(changed_roles)
            and all(
                (dataset_ids or {}).get(role)
                == self.state["resources"].get("dataset:" + role, {}).get("id")
                and (dataset_ids or {}).get(role) is not None
                for role in changed_roles
            )
        )
        identity_keys = {"folder_path", "source_tables"}
        same_scope = (same_sources or expanding_sources or owned_source_update) and {
            key: value for key, value in previous.items() if key not in identity_keys
        } == {key: value for key, value in target.items() if key not in identity_keys}
        folder_change = (
            allow_folder_move
            and "folder_path" in previous
            and "folder_path" in target
            and same_scope
        )
        compatible_target = same_scope and previous.get("folder_path") == target.get("folder_path")
        if self.state.get("version") != 1 or (
            previous != target and not folder_change and not compatible_target
        ):
            message = (
                "resources.json belongs to another organization, folder, or "
                "source. Keep it with its original recipe."
            )
            raise DataLensUtilsError(message)

    def set_folders(self, folders: Any, *, entries: Any = None) -> Any:
        self.folders = folders
        if entries is None:
            by_folder = {
                folder.key: list(folder.list_entries())
                for folder in {folder.key: folder for folder in folders.values()}.values()
            }
        else:
            by_folder = {
                folder.key: [
                    entry
                    for entry in entries
                    if (entry.key or "").rstrip("/").rpartition("/")[0] == folder.key.rstrip("/")
                ]
                for folder in folders.values()
            }
        self.entries = {scope: by_folder[folder.key] for scope, folder in folders.items()}

    def folder_for(self, scope: Any) -> Any:
        return self.folders[scope]

    def seed_configured_ids(self, definitions: Any) -> Any:
        """Share durable identities through object JSONs without sharing auth."""
        changed = False
        for key, definition in definitions.items():
            identifier = definition.get("id")
            if not identifier:
                continue
            existing = self.state["resources"].get(key)
            if existing and existing["id"] != identifier:
                message = (
                    f"Configured ID and checkpoint disagree for {key!r}; no resource was replaced."
                )
                raise DataLensUtilsError(message)
            if existing is None:
                self.state["resources"][key] = {
                    "id": identifier,
                    "name": definition["name"],
                    "scope": definition["scope"],
                }
                changed = True
        if changed:
            self._save()

    def _save(self) -> Any:
        descriptor, temporary = tempfile.mkstemp(
            dir=self.path.parent, prefix=".resources-", suffix=".json"
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(self.state, stream, indent=2, ensure_ascii=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            Path(temporary).replace(self.path)
        finally:
            if Path(temporary).exists():
                Path(temporary).unlink()

    @contextmanager
    def record_mutations(self, recorder: Any) -> Iterator[Any]:
        previous = self.mutation_recorder
        self.mutation_recorder = recorder
        try:
            yield
        finally:
            self.mutation_recorder = previous

    def checkpoint(self, key: Any, entity: Any, *, scope: Any = None, mutation: Any = False) -> Any:
        entry = self.state["resources"].setdefault(key, {})
        entry.update(id=entity.id, name=entity.name)
        if scope is not None:
            entry["scope"] = scope
        if entity.key:
            entry["folder_path"] = entity.key.rstrip("/").rpartition("/")[0]
        self._save()
        if mutation and self.mutation_recorder is not None:
            self.mutation_recorder(key, entity)

    def complete_target(self) -> Any:
        """Commit a new parent only after all configured resources are verified."""
        if self.state["target"] != self.desired_target:
            self.state["target"] = self.desired_target
            self._save()

    def phase(
        self,
        key: Any,
        phase: Any,
        *,
        pending_items: Any = None,
        managed_items: Any = None,
        managed_aliases: Any = None,
    ) -> Any:
        entry = self.state["resources"][key]
        entry["phase"] = phase
        if pending_items is not None:
            entry["pending_items"] = list(pending_items)
        if managed_items is not None:
            entry["managed_items"] = list(managed_items)
        if managed_aliases is not None:
            entry["managed_aliases"] = managed_aliases
        self._save()

    def verify_location(self, entity: Any, name: Any = None, *, scope: Any = None) -> Any:
        if scope is None:
            scope = next(
                (
                    entry["scope"]
                    for entry in self.state["resources"].values()
                    if entry["id"] == entity.id
                ),
                "dash",
            )
        key = (entity.key or "").rstrip("/")
        parent, _, actual_name = key.rpartition("/")
        if entity.workbook_id is not None or parent != self.folder_for(scope).key.rstrip("/"):
            message = f"Resource {entity.id} is outside the configured folder."
            raise DataLensUtilsError(message)
        if name is not None and actual_name != name:
            message = f"Resource {entity.id} has an unexpected name: {actual_name!r}."
            raise DataLensUtilsError(message)

    def existing(
        self, key: Any, name: Any, getter: Any, *, scope: Any, refresh: Any = False
    ) -> Any:
        entry = self.state["resources"].get(key)
        if entry:
            if entry["scope"] != scope:
                message = f"The resource kind for {key!r} changed; keep its semantic key stable."
                raise DataLensUtilsError(message)
            entity = getter(by_id=entry["id"])
            parent = (entity.key or "").rstrip("/").rpartition("/")[0]
            desired_parent = self.folder_for(scope).key.rstrip("/")
            current_root = self.folder.key.rstrip("/")
            allowed_sources = {current_root} if scope != "dash" else set()
            if self.allow_folder_move:
                previous_root = self.state["target"]["folder_path"].rstrip("/")
                suffix = desired_parent[len(current_root) :]
                allowed_sources.update((previous_root, previous_root + suffix))
                recorded_parent = entry.get("folder_path")
                if recorded_parent and (
                    recorded_parent == previous_root
                    or recorded_parent.startswith(previous_root + "/")
                ):
                    allowed_sources.add(recorded_parent)
            if parent != desired_parent and parent in allowed_sources:
                # Move only checkpointed objects within their recorded source
                # boundary. The durable ID survives even a failed move fetch.
                if entity.workbook_id is not None:
                    message = f"Resource {entity.id} is outside the configured folder."
                    raise DataLensUtilsError(message)
                entity = entity.move(self.folder_for(scope))
                self.checkpoint(key, entity, scope=scope, mutation=True)
                entity = getter(by_id=entity.id)
        else:
            entries = (
                list(self.folder_for(scope).list_entries()) if refresh else self.entries[scope]
            )
            matches = [
                item
                for item in entries
                if item.scope == scope and item.name.rsplit("/", 1)[-1] == name
            ]
            if len(matches) > 1:
                message = f"Multiple {scope} resources named {name!r} in the destination."
                raise DataLensUtilsError(message)
            if not matches:
                return None
            entity = getter(by_id=matches[0].id)
        self.verify_location(entity, scope=scope)
        self.checkpoint(key, entity, scope=scope)
        return entity

    def create(self, key: Any, name: Any, builder: Any, getter: Any, *, scope: Any) -> Any:
        try:
            entity = builder.build()
        except ConflictError:
            entity = self.existing(key, name, getter, scope=scope, refresh=True)
            if entity is None:
                raise
        self.checkpoint(key, entity, scope=scope, mutation=True)
        entity = getter(by_id=entity.id)
        self.verify_location(entity, name)
        self.checkpoint(key, entity, scope=scope)
        return entity

    def rename(self, key: Any, entity: Any, name: Any, getter: Any) -> Any:
        self.verify_location(entity)
        if entity.name == name:
            return entity
        entity = entity.rename(name)
        self.checkpoint(key, entity, mutation=True)
        entity = getter(by_id=entity.id)
        self.verify_location(entity, name)
        self.checkpoint(key, entity)
        return entity

    def persisted(self, key: Any, entity: Any, getter: Any, *, branch: Any = "published") -> Any:
        self.checkpoint(key, entity, mutation=True)
        entity = getter(by_id=entity.id, branch=branch)
        self.verify_location(entity)
        self.checkpoint(key, entity)
        return entity
