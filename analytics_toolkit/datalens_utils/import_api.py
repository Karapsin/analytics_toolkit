"""SDK-free entry point for importing a published dashboard into a new project."""

from __future__ import annotations

import os
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .deployment import BIProjectDeployment, TargetLocation
from .errors import DataLensConfigurationError
from .session import ProjectPaths, Session, bind_session


@dataclass(frozen=True)
class DataLensAuthConfig:
    """Existing authentication references; no credentials or environment setup."""

    installation: str = "yc"
    organization_id: str | None = None
    yc_profile: str | None = None
    yc_binary: str = "yc"
    base_url: str | None = None
    token_env: str | None = None

    def __post_init__(self) -> None:
        self.deployment("Import", TargetLocation.path("/"))

    def deployment(self, name: str, target: TargetLocation) -> BIProjectDeployment:
        return BIProjectDeployment(name, target, **asdict(self))


def dashboard_id(source: str, auth: DataLensAuthConfig) -> str:
    if not isinstance(source, str) or not source.strip():
        msg = "Source must be a dashboard URL or ID."
        raise DataLensConfigurationError(msg)
    if "://" not in source:
        identifier = source
    else:
        parsed = urlsplit(source)
        hosts = {"datalens.ru", "datalens.yandex.ru", "datalens.yandex.cloud"}
        if auth.installation == "enterprise":
            host = urlsplit(auth.base_url or "").hostname
            hosts = {host} if host else set()
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.hostname not in hosts
            or parsed.username
            or parsed.password
        ):
            msg = "Dashboard URL does not match the configured installation."
            raise DataLensConfigurationError(msg)
        parts = parsed.path.strip("/").split("/")
        if len(parts) == 1:
            identifier = parts[0].split("-", 1)[0]
        elif len(parts) == 2 and parts[0] in {"dashboards", "dashboard"}:  # noqa: PLR2004 - URL segments.
            identifier = parts[1].split("-", 1)[0]
        else:
            msg = "Unsupported dashboard URL path."
            raise DataLensConfigurationError(msg)
    if not re.fullmatch(r"[A-Za-z0-9_-]+", identifier):
        msg = "Invalid dashboard ID."
        raise DataLensConfigurationError(msg)
    return identifier


def import_dashboard(  # noqa: PLR0913 - Public import contract.
    source: str,
    destination: str | Path,
    *,
    auth: DataLensAuthConfig,
    runtime_root: str | Path | None = None,
    tab_ids: Any = None,
    dry_run: bool = True,
    resume: bool = False,
    client_factory: Any = None,
    reporter: Any = None,
) -> dict[str, Any]:
    """Capture published source and preview/write a new local project; no cloud writes."""
    from .project import require_sdk  # noqa: PLC0415 - Optional SDK boundary.

    require_sdk()
    if not isinstance(auth, DataLensAuthConfig):
        msg = "auth must be DataLensAuthConfig."
        raise DataLensConfigurationError(msg)
    identifier = dashboard_id(source, auth)
    root = Path(destination).expanduser().resolve()
    runtime = Path(
        runtime_root
        or os.environ.get("DATALENS_RUNTIME_DIR")
        or root.parent / ".local" / root.name.replace(" ", "")
    )
    paths = ProjectPaths(root, runtime)
    if root.exists() and (not root.is_dir() or any(root.iterdir())):
        msg = "New import destination must be absent or empty; use project import methods to merge."
        raise DataLensConfigurationError(msg)
    if tab_ids is not None and (
        isinstance(tab_ids, str) or not all(isinstance(item, str) and item for item in tab_ids)
    ):
        msg = "tab_ids must be a sequence of nonempty tab IDs."
        raise DataLensConfigurationError(msg)
    from .auth.client import datalens_client  # noqa: PLC0415 - Optional SDK boundary.
    from .import_capture import Capture  # noqa: PLC0415 - Optional SDK boundary.
    from .import_materialize import materialize  # noqa: PLC0415 - Optional SDK boundary.
    from .import_source import atomic_json, location  # noqa: PLC0415 - Optional SDK boundary.

    state = Session(
        paths,
        auth.deployment("Import", TargetLocation.path("/")),
        {
            "schema_version": 2,
            "installation": auth.installation,
            "organization_id": auth.organization_id,
            "yc_profile": auth.yc_profile,
            "request_interval_seconds": 1.2,
            "_request_count": 0,
        },
        client_factory=client_factory,
        reporter=reporter,
    )
    started = time.perf_counter()
    with bind_session(state), datalens_client() as client:
        capture = Capture(client, resume=resume)
        with capture.failures():
            selected = capture.discover(identifier, tab_ids)
            dashboard = capture.entities[identifier]
            target = location(dashboard)
            if target is None:
                msg = "Source dashboard location is unavailable."
                failure = DataLensConfigurationError(msg)
                failure.resource_key = "dashboard:" + identifier
                raise failure
            state.deployment = auth.deployment(dashboard.name, TargetLocation(**target))
            capture.finish()
            report = materialize(capture, identifier, selected, auth, dry_run=dry_run)
        report.update(
            operation="import-dashboard",
            dry_run=dry_run,
            request_count=state.runtime["_request_count"],
            cache_hits=capture.cache_hits,
            elapsed_seconds=time.perf_counter() - started,
            runtime_root=str(paths.runtime_root),
            messages=state.messages,
        )
        atomic_json(paths.runtime_root / "import-report.json", report)
        return report
