"""Ensure the named chart and dataset folders through public SDK operations."""

from __future__ import annotations

from typing import Any

from datalens_sdk import ConflictError, NotFoundError

from analytics_toolkit.datalens_utils.errors import DataLensUtilsError


def ensure_target_folder(*, client: Any, path: Any) -> Any:
    """Reuse a target or create missing descendants of an existing DL root."""
    path = path.strip("/")
    if not path or any(part in ("", ".", "..") for part in path.split("/")):
        message = (
            "TARGET_PATH must identify a DataLens folder without empty or relative components."
        )
        raise DataLensUtilsError(message) from None
    try:
        folder = client.get.folder(by_path=path)
    except NotFoundError:
        parent_path, separator, name = path.rpartition("/")
        if not separator:
            message = (
                f"DataLens root {path!r} does not exist; select a target beneath an existing root."
            )
            raise DataLensUtilsError(message) from None
        parent = ensure_target_folder(client=client, path=parent_path)
        try:  # noqa: SIM105
            client.create.folder(name=name, location=parent).build()
        except ConflictError:
            # A concurrent run may have created this exact folder.
            pass
        folder = client.get.folder(by_path=path)
    if folder.key.strip("/").casefold() != path.casefold():
        message = f"The returned target folder differs from TARGET_PATH: {folder.key!r}."
        raise DataLensUtilsError(message) from None
    return folder


def ensure_resource_folders(*, client: Any, parent: Any, names: Any) -> Any:
    entries = list(parent.list_entries())
    folders = {"dash": parent}
    for role, scope in (("charts", "widget"), ("datasets", "dataset")):
        name = names[role]
        if not name or name in (".", "..") or "/" in name:
            message = f"The {role} folder name must be a single path component."
            raise DataLensUtilsError(message) from None
        path = f"{parent.key.rstrip('/')}/{name}"
        matches = [
            entry
            for entry in entries
            if entry.scope == "folder" and entry.name.rstrip("/").rsplit("/", 1)[-1] == name
        ]
        if len(matches) > 1:
            message = f"Multiple folders named {name!r} in the destination."
            raise DataLensUtilsError(message) from None
        if not matches:
            try:  # noqa: SIM105
                client.create.folder(name=name, location=parent).build()
            except ConflictError:
                # Another run may have created the same exact folder meanwhile.
                pass
        folder = client.get.folder(by_path=path)
        if folder.key.rstrip("/") != path:
            message = f"The {role} folder is outside the configured destination."
            raise DataLensUtilsError(message) from None
        folders[scope] = folder
    return folders
