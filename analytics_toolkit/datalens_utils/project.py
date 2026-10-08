"""Dependency-free public facade; SDK imports are delayed until an operation."""

from __future__ import annotations

import sys
import time
from contextlib import contextmanager
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, ContextManager, Iterator, Sequence

from .errors import DataLensConfigurationError, DataLensDependencyError
from .session import Deployment, ProjectPaths, Session, bind_session


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
    if installed != "3.1.0":
        raise DataLensDependencyError(
            "This adapter version requires datalens-sdk==3.1.0; found " + installed + "."
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
        deployment: Deployment,
        *,
        client_factory: Callable[[Session], ContextManager[Any]] | None = None,
        reporter: Callable[..., None] | None = None,
    ) -> None:
        self.paths = ProjectPaths(Path(project_root), Path(runtime_root))
        if not isinstance(deployment, Deployment):
            message = "deployment must be a Deployment instance."
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
        ui: bool = False,
        branch: str = "published",
        reporter: Callable[..., None] | None = None,
    ) -> dict[str, Any]:
        if sum(bool(value) for value in (chart_keys, dataset_keys, ui)) > 1:
            message = "Choose chart_keys, dataset_keys, or ui, not multiple scopes."
            raise DataLensConfigurationError(message)
        if branch not in {"published", "saved"}:
            message = "branch must be published or saved."
            raise DataLensConfigurationError(message)
        started = time.perf_counter()
        with self.session(reporter=reporter) as state:
            from .settings import validate_deployment  # noqa: PLC0415

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
                from .editing.commands import run  # noqa: PLC0415

                arguments = SimpleNamespace(
                    command=command,
                    chart=chart_keys,
                    dataset=dataset_keys,
                    ui=ui,
                    branch=branch,
                    full=True,
                )
                report = run(arguments, self.deployment.as_dict()) or {}
            return {
                "operation": command,
                **report,
                "request_count": state.runtime.get("_request_count", 0),
                "elapsed_seconds": time.perf_counter() - started,
                "messages": list(state.messages),
            }

    def validate(self) -> dict[str, Any]:
        """Validate local configuration without cloud requests or runtime writes."""
        return self._execute("validate")

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
        ui: bool = False,
        branch: str = "published",
    ) -> dict[str, Any]:
        """Import supported remote changes using the verified three-way baseline."""
        return self._execute(
            "pull", chart_keys=chart_keys, dataset_keys=dataset_keys, ui=ui, branch=branch
        )

    def apply(
        self,
        *,
        chart_keys: Sequence[str] | None = None,
        dataset_keys: Sequence[str] | None = None,
        ui: bool = False,
    ) -> dict[str, Any]:
        """Apply affected local changes with revision/draft conflict protection."""
        return self._execute("apply", chart_keys=chart_keys, dataset_keys=dataset_keys, ui=ui)

    def verify(self, *, full: bool = True) -> dict[str, Any]:
        """Verify full published metadata and references; this is not numeric evaluation."""
        if full is not True:
            message = "Only full metadata verification is supported."
            raise DataLensConfigurationError(message)
        return self._execute("verify")
