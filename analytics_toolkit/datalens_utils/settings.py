"""Company-shareable configuration; generated runtime files live elsewhere."""

from __future__ import annotations

import json
import platform
import re
from pathlib import Path
from shutil import which
from typing import Any

from .errors import DataLensConfigurationError
from .session import current_session as session


def read_config(name: Any) -> Any:
    """Read an editable configuration relative to this project, not the shell."""
    return json.loads((session().paths.project_root / "configs" / name).read_text(encoding="utf-8"))


def read_chart_definitions() -> Any:
    """One JSON per chart/Editor selector, grouped by family and renderer."""
    definitions = {}
    for directory in ("DL objects/charts", "DL objects/selectors"):
        for path in sorted((session().paths.project_root / "configs" / directory).rglob("*.json")):
            definition = json.loads(path.read_text(encoding="utf-8"))
            key = definition.get("key", path.stem)
            if key in definitions:
                message = f"Duplicate chart semantic key {key!r}."
                raise DataLensConfigurationError(message)
            if not all(definition.get(field) for field in ("family", "type", "name", "title")):
                message = (
                    f"Incomplete chart definition: {path.relative_to(session().paths.project_root)}"
                )
                raise DataLensConfigurationError(message)
            definitions[key] = definition
    return definitions


def asset_path(relative: Any) -> Any:
    """Only project-contained files may supply shared SQL and JavaScript."""
    path = (session().paths.project_root / relative).resolve()
    if not path.is_relative_to(session().paths.project_root) or not path.is_file():
        message = f"Missing or non-project asset: {relative!r}."
        raise DataLensConfigurationError(message)
    return path


def source_tables(definitions: Any) -> Any:
    """Read actual existing source tables from the dataset recipe."""
    tables = {}
    for role, definition in definitions.items():
        value = definition.get("table", "")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*", value):
            message = f"Configure {role!r} as an ordinary database.table identifier."
            raise DataLensConfigurationError(message)
        tables[role] = value
    return tables


def validate_deployment(*, target_path: Any, connection_name: Any, connection_id: Any) -> Any:
    missing = [
        name
        for name, value in (
            ("organization_id", session().runtime.get("organization_id")),
            ("yc_profile", session().runtime.get("yc_profile")),
            ("target_path", target_path),
            ("connection_name", connection_name),
            ("connection_id", connection_id),
        )
        if not value
    ]
    if missing:
        message = f"Set {', '.join(missing)} using the constants at the top of dashboard.py."
        raise DataLensConfigurationError(message)
    if (
        session().paths.runtime_root == session().paths.project_root
        or session().paths.project_root in session().paths.runtime_root.parents
    ):
        message = "DATALENS_RUNTIME_DIR must be outside the shareable project folder."
        raise DataLensConfigurationError(message)


def platform_key() -> Any:
    system = {"Darwin": "darwin", "Linux": "linux", "Windows": "windows"}.get(platform.system())
    architecture = {"x86_64": "amd64", "amd64": "amd64", "arm64": "arm64", "aarch64": "arm64"}.get(
        platform.machine().lower()
    )
    if system is None or architecture is None or (system == "windows" and architecture != "amd64"):
        message = "Supported: macOS/Linux x86-64 or ARM64, and Windows x86-64."
        raise DataLensConfigurationError(message)
    return f"{system}-{architecture}"


def yc_binary() -> Any:
    """Use the caller's CLI, rather than a package-relative managed installation."""
    value = session().deployment.yc_binary
    binary = which(str(value))
    if binary is None:
        message = (
            "Configured yc CLI is missing. Select an existing executable via Deployment.yc_binary."
        )
        raise DataLensConfigurationError(message)
    return Path(binary)
