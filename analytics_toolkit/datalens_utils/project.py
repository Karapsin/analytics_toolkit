"""Dependency-free public facade; SDK imports are delayed until an operation."""

from __future__ import annotations

import sys
import time
from contextlib import contextmanager
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, ContextManager, Iterator, Sequence

from .capabilities import TESTED_SDK_VERSIONS, get_capabilities
from .deployment import BIProjectDeployment
from .errors import DataLensConfigurationError, DataLensDependencyError
from .session import Deployment, ProjectPaths, Session, bind_session

BI_RECIPE_VERSION = 2


def require_sdk() -> None:
    namespace = __package__
    extra = (
        "analytics-toolkit[datalens]"
        if namespace.startswith("analytics_toolkit.")
        else "datalens-utils[datalens]"
    )
    if sys.version_info < (3, 10):
        message = (
            "DataLens operations require Python 3.10 or newer; base imports support Python 3.8+."
        )
        raise DataLensDependencyError(message)
    try:
        installed = version("datalens-sdk")
    except PackageNotFoundError:
        raise DataLensDependencyError(
            "Install the optional dependencies with: pip install '" + extra + "'"
        ) from None
    if installed not in TESTED_SDK_VERSIONS:
        raise DataLensDependencyError(
            "This adapter requires datalens-sdk 3.1.0 or 3.2.0; found " + installed + "."
        )


class DataLensProject:
    """Manage one recipe with isolated deployment state and optional client injection.

    Constructing or importing the facade performs no project reads, login, setup,
    or writes. Each operation reads the current files and creates its own Session.
    """

    def __init__(
        self,
        project_root: str | Path,
        runtime_root: str | Path,
        deployment: Deployment | BIProjectDeployment,
        *,
        client_factory: Callable[[Session], ContextManager[Any]] | None = None,
        reporter: Callable[..., None] | None = None,
    ) -> None:
        self.paths = ProjectPaths(Path(project_root), Path(runtime_root))
        if not isinstance(deployment, (Deployment, BIProjectDeployment)):
            message = "deployment must be a Deployment or BIProjectDeployment instance."
            raise DataLensConfigurationError(message)
        self.deployment = deployment
        self.client_factory = client_factory
        self.reporter = reporter

    @contextmanager
    def session(self, *, reporter: Callable[..., None] | None = None) -> Iterator[Session]:
        require_sdk()
        state = Session.load(
            self.paths,
            self.deployment,
            client_factory=self.client_factory,
            reporter=self.reporter if reporter is None else reporter,
        )
        with bind_session(state):
            yield state

    def _execute(  # noqa: PLR0913
        self,
        command: str,
        *,
        chart_keys: Sequence[str] | None = None,
        dataset_keys: Sequence[str] | None = None,
        resource_keys: Sequence[str] | None = None,
        ui: bool = False,
        branch: str = "published",
        reporter: Callable[..., None] | None = None,
    ) -> dict[str, Any]:
        if (
            sum(
                value is not None and value is not False
                for value in (chart_keys, dataset_keys, resource_keys, ui)
            )
            > 1
        ):
            message = "Choose chart_keys, dataset_keys, ui, or resource_keys, not multiple scopes."
            raise DataLensConfigurationError(message)
        if branch not in {"published", "saved"}:
            message = "branch must be published or saved."
            raise DataLensConfigurationError(message)
        started = time.perf_counter()
        if command == "capabilities":
            return {
                "operation": command,
                **self.capabilities(),
                "request_count": 0,
                "elapsed_seconds": time.perf_counter() - started,
                "messages": [],
            }
        with self.session(reporter=reporter) as state:
            if command == "plan":
                from .planning import plan  # noqa: PLC0415 - Lazy SDK boundary or module cycle.

                report = plan(resource_keys=resource_keys)
                return {
                    "operation": command,
                    **report,
                    "request_count": state.runtime.get("_request_count", 0),
                    "elapsed_seconds": time.perf_counter() - started,
                    "messages": list(state.messages),
                }
            if state.runtime.get("schema_version", 1) == BI_RECIPE_VERSION:
                from .bi_engine import (  # noqa: PLC0415 - Keep the public facade SDK-free.
                    run as run_bi,
                )

                keys = resource_keys
                if chart_keys is not None:
                    keys = ["chart:" + key for key in chart_keys]
                elif dataset_keys is not None:
                    keys = ["dataset:" + key for key in dataset_keys]
                elif ui:
                    keys = ["dashboard:main"]
                report = run_bi(command, resource_keys=keys, branch=branch)
                return {
                    "operation": command,
                    **report,
                    "request_count": state.runtime.get("_request_count", 0),
                    "elapsed_seconds": time.perf_counter() - started,
                    "messages": list(state.messages),
                }
            report = self._run_legacy(
                command,
                SimpleNamespace(
                    chart=chart_keys,
                    dataset=dataset_keys,
                    resource=resource_keys,
                    ui=ui,
                    branch=branch,
                ),
            )
            return {
                "operation": command,
                **report,
                "request_count": state.runtime.get("_request_count", 0),
                "elapsed_seconds": time.perf_counter() - started,
                "messages": list(state.messages),
            }

    def _run_legacy(self, command: str, arguments: Any) -> dict[str, Any]:
        from .settings import validate_deployment  # noqa: PLC0415

        if not isinstance(self.deployment, Deployment):
            message = "BIProjectDeployment requires recipe schema_version 2."
            raise DataLensConfigurationError(message)
        validate_deployment(
            target_path=self.deployment.target_path,
            connection_name=self.deployment.connection_name,
            connection_id=self.deployment.connection_id,
        )
        if command == "validate":
            from .engine import validate  # noqa: PLC0415

            report = {"coverage": validate()}
        elif command == "reconcile":
            from .engine import reconcile  # noqa: PLC0415

            report = reconcile()
        else:
            from .editing.commands import run as run_edit  # noqa: PLC0415

            arguments = SimpleNamespace(
                command=command,
                chart=arguments.chart,
                dataset=arguments.dataset,
                resource=arguments.resource,
                ui=arguments.ui,
                branch=arguments.branch,
                full=True,
            )
            report = run_edit(arguments, self.deployment.as_dict()) or {}
        return report

    def validate(self) -> dict[str, Any]:
        """Validate local configuration without cloud requests or runtime writes."""
        return self._execute("validate")

    def import_tab(
        self, *, dashboard_id: str, tab_id: str, dry_run: bool = True, upgrade_recipe: bool = False
    ) -> dict[str, Any]:
        """Preview or merge one published tab and its dependency closure."""
        return self._import(
            dashboard_id=dashboard_id, tab_id=tab_id, dry_run=dry_run, upgrade_recipe=upgrade_recipe
        )

    def import_dashboard(
        self, *, dashboard_id: str, dry_run: bool = True, upgrade_recipe: bool = False
    ) -> dict[str, Any]:
        """Preview or merge every published tab and shared dependency."""
        return self._import(
            dashboard_id=dashboard_id, tab_id=None, dry_run=dry_run, upgrade_recipe=upgrade_recipe
        )

    def _import(
        self,
        *,
        dashboard_id: str,
        tab_id: str | None,
        dry_run: bool,
        upgrade_recipe: bool,
        reporter: Callable[..., None] | None = None,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        with self.session(reporter=reporter) as state:
            from .bi_import import (  # noqa: PLC0415 - Keep the public facade SDK-free.
                import_recipe,
            )

            report = import_recipe(
                dashboard_id=dashboard_id,
                tab_id=tab_id,
                dry_run=dry_run,
                upgrade_recipe=upgrade_recipe,
            )
            return {
                "operation": "import-tab" if tab_id is not None else "import-dashboard",
                **report,
                "request_count": state.runtime.get("_request_count", 0),
                "elapsed_seconds": time.perf_counter() - started,
                "messages": list(state.messages),
            }

    def capabilities(self) -> dict[str, Any]:
        """Inspect local public SDK capabilities; no project reads or cloud requests."""
        return get_capabilities(installation=getattr(self.deployment, "installation", "yc"))

    def plan(self, *, resource_keys: Sequence[str] | None = None) -> dict[str, Any]:
        """Read local state and remote metadata to propose actions without writes."""
        return self._execute("plan", resource_keys=resource_keys)

    def reconcile(self) -> dict[str, Any]:
        """Create/reconcile all managed resources, verify, export, and record IDs."""
        return self._execute("reconcile")

    def status(self) -> dict[str, Any]:
        """Compare local files with one batched revision inventory; no cloud writes."""
        return self._execute("status")

    def pull(
        self,
        *,
        chart_keys: Sequence[str] | None = None,
        dataset_keys: Sequence[str] | None = None,
        resource_keys: Sequence[str] | None = None,
        ui: bool = False,
        branch: str = "published",
    ) -> dict[str, Any]:
        """Import supported remote changes using the verified three-way baseline."""
        return self._execute(
            "pull",
            chart_keys=chart_keys,
            dataset_keys=dataset_keys,
            resource_keys=resource_keys,
            ui=ui,
            branch=branch,
        )

    def apply(
        self,
        *,
        chart_keys: Sequence[str] | None = None,
        dataset_keys: Sequence[str] | None = None,
        resource_keys: Sequence[str] | None = None,
        ui: bool = False,
    ) -> dict[str, Any]:
        """Apply affected local changes with revision/draft conflict protection."""
        return self._execute(
            "apply",
            chart_keys=chart_keys,
            dataset_keys=dataset_keys,
            resource_keys=resource_keys,
            ui=ui,
        )

    def verify(self, *, full: bool = True) -> dict[str, Any]:
        """Verify full published metadata and references; this is not numeric evaluation."""
        if full is not True:
            message = "Only full metadata verification is supported."
            raise DataLensConfigurationError(message)
        return self._execute("verify")
