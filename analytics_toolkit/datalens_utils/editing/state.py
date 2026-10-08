from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from analytics_toolkit.datalens_utils.errors import DataLensUtilsError
from analytics_toolkit.datalens_utils.session import current_session as session
from analytics_toolkit.datalens_utils.settings import read_chart_definitions, read_config


def fingerprint(value: Any) -> Any:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def configuration() -> Any:
    # Cache editable inputs, not reconstructed SDK entities. Assets participate
    # in the hash, including files shared by several charts.
    units = {}
    datasets = read_config("DL objects/datasets.json")
    charts = read_chart_definitions()
    chart_paths = {}
    for directory in ("charts", "selectors"):
        for path in (session().paths.project_root / "configs/DL objects" / directory).rglob(
            "*.json"
        ):
            value = json.loads(path.read_text(encoding="utf-8"))
            chart_paths[value.get("key", path.stem)] = path.relative_to(
                session().paths.project_root
            ).as_posix()
    for kind, definitions in (("dataset", datasets), ("chart", charts)):
        for key, definition in definitions.items():
            files = {}
            if kind == "chart":
                files[chart_paths[key]] = definition
            else:
                files["configs/DL objects/datasets.json"] = {key: definition}
            assets = list(definition.get("scripts", {}).values())
            assets += [
                definition[name]
                for name in ("projection_file", "query_file")
                if definition.get(name)
            ]
            for relative in assets:
                path = (session().paths.project_root / relative).resolve()
                if not path.is_relative_to(session().paths.project_root.resolve()):
                    message = f"Asset is outside the project: {relative}"
                    raise DataLensUtilsError(message)
                files[relative] = path.read_text(encoding="utf-8")
            units[f"{kind}:{key}"] = {"id": definition.get("id"), "files": files}
    ui = {
        path.relative_to(session().paths.project_root).as_posix(): json.loads(
            path.read_text(encoding="utf-8")
        )
        for path in (session().paths.project_root / "configs/UI").rglob("*.json")
    }
    ui["configs/DL objects/dashboard.json"] = read_config("DL objects/dashboard.json")
    # Widget titles live in chart JSONs but also affect dashboard content.
    ui["chart_titles"] = {key: value["title"] for key, value in charts.items()}
    units["dashboard"] = {"id": read_config("DL objects/dashboard.json").get("id"), "files": ui}
    for unit in units.values():
        unit["fingerprint"] = fingerprint(unit["files"])
    return units


def metadata(entry: Any) -> Any:
    return {
        "id": entry.id,
        "saved_id": entry.saved_id,
        "published_id": entry.published_id,
        "path": entry.key,
    }


def inventory(client: Any, units: Any) -> Any:
    ids = [unit["id"] for unit in units.values() if unit["id"]]
    if len(ids) != len(set(ids)):
        message = "Configured resource IDs must be unique."
        raise DataLensUtilsError(message)
    entries = (
        {entry.id: entry for entry in client.navigation.get_entries(ids=ids, page_size=100)}
        if ids
        else {}
    )
    return {key: entries.get(unit["id"]) for key, unit in units.items()}


class EditState:
    def __init__(self, target: Any, *, path: Any = None, reset: Any = False) -> None:
        self.path = path or session().paths.runtime_root / "edit-state.json"
        self.target = target
        self.value = (
            json.loads(self.path.read_text(encoding="utf-8"))
            if self.path.exists()
            else {"version": 1, "target": target, "resources": {}}
        )
        if reset and self.value.get("target") != target:
            self.value = {"version": 1, "target": target, "resources": {}}
        if self.value.get("version") != 1 or self.value.get("target") != target:
            message = (
                "Editing cache belongs to another deployment. Run the full entry "
                "point to establish its baseline."
            )
            raise DataLensUtilsError(message)

    def changes(self, units: Any, entries: Any, *, branch: Any = "published") -> Any:
        result = {}
        for key, unit in units.items():
            old, entry = self.value["resources"].get(key), entries.get(key)
            if entry is None:
                result[key] = "missing"
            elif old is None:
                result[key] = "untracked"
            else:
                live = metadata(entry)
                remote = (
                    live[branch + "_id"] != old.get(branch + "_id")
                    or live[branch + "_id"] is None
                    or live["path"] != old.get("path")
                    or live["id"] != old.get("id")
                )
                local = unit["fingerprint"] != old["fingerprint"]
                if old.get("branch") == "saved" and live["saved_id"] != live["published_id"]:
                    local = True
                    remote = live["saved_id"] != old.get("saved_id") or live["path"] != old.get(
                        "path"
                    )
                result[key] = (
                    "local"
                    if self.owns_write(key, unit, entry)
                    else (
                        "both"
                        if local and remote
                        else "local"
                        if local
                        else "remote"
                        if remote
                        else "unchanged"
                    )
                )
        return result

    def owns_write(self, key: Any, unit: Any, entry: Any) -> Any:
        pending = self.value.get("pending_apply", {}).get(key, {})
        written = pending.get("written")
        return bool(
            entry is not None
            and written
            and written.get("saved_id")
            and written.get("published_id")
            and pending.get("fingerprint") == unit["fingerprint"]
            and metadata(entry) == written
        )

    def begin_apply(self, units: Any, entries: Any, selected: Any) -> Any:
        pending = self.value.setdefault("pending_apply", {})
        for key in selected:
            if not self.owns_write(key, units[key], entries[key]):
                pending[key] = {
                    "fingerprint": units[key]["fingerprint"],
                    "base": metadata(entries[key]),
                }
        self.save()

    def record_write(self, key: Any, entity: Any) -> Any:
        pending = self.value.get("pending_apply", {}).get(key)
        if pending is None:
            message = f"Unplanned mutation of {key}; the resource checkpoint was preserved."
            raise DataLensUtilsError(message)
        pending["written"] = metadata(entity)
        self.save()

    def remember(
        self,
        key: Any,
        unit: Any,
        entity: Any,
        *,
        branch: Any = "published",
        baseline_files: Any = None,
    ) -> Any:
        value = {
            **metadata(entity),
            "files": baseline_files if baseline_files is not None else unit["files"],
        }
        value["fingerprint"] = fingerprint(value["files"])
        # Pulling a saved draft must still require an apply to publish it.
        value["branch"] = branch
        self.value["resources"][key] = value
        self.value.get("pending_apply", {}).pop(key, None)

    def save(self) -> Any:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            dir=self.path.parent, prefix=".edit-state-", suffix=".json"
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(self.value, stream, indent=2, ensure_ascii=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            Path(temporary).replace(self.path)
        finally:
            if Path(temporary).exists():
                Path(temporary).unlink()


MISSING = object()


def merge(base: Any, local: Any, remote: Any, path: Any = "") -> Any:
    # Three-way merge; ordered arrays and text assets are indivisible.
    if local in (base, remote):
        return remote
    if remote == base:
        return local
    if all(isinstance(value, dict) for value in (base, local, remote)):
        merged = {}
        for key in dict.fromkeys([*base, *local, *remote]):
            value = merge(
                base.get(key, MISSING),
                local.get(key, MISSING),
                remote.get(key, MISSING),
                f"{path}/{key}",
            )
            if value is not MISSING:
                merged[key] = value
        return merged
    message = f"Local and DataLens changes conflict at {path}. Neither version was overwritten."
    raise DataLensUtilsError(message)


def write_files(files: Any) -> Any:
    # All extraction and merge checks finish before the first local replacement.
    for relative in files:
        path = (session().paths.project_root / relative).resolve()
        if not path.is_relative_to(session().paths.project_root.resolve()) or not path.is_file():
            message = f"Pull cannot write an unknown project file: {relative}"
            raise DataLensUtilsError(message)
    for relative, value in files.items():
        path = session().paths.project_root / relative
        if relative.endswith(".json"):
            previous = path.read_text(encoding="utf-8")
            if json.loads(previous) == value:
                continue
            indent = 4 if '\n    "' in previous and '\n  "' not in previous else 2
            text = json.dumps(value, indent=indent, ensure_ascii=False) + "\n"
        else:
            text = value
        if path.read_text(encoding="utf-8") != text:
            temporary = path.with_suffix(path.suffix + ".tmp")
            temporary.write_text(text, encoding="utf-8")
            temporary.replace(path)
