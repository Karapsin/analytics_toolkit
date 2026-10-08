"""Explicit project state with context-local binding for the reusable adapters.

The facade supplies a Session for each operation. Binding never changes module
roots or process environment, and nested operations restore the previous scope.
"""

from __future__ import annotations

import json
import math
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Iterator

from .errors import DataLensConfigurationError


@dataclass(frozen=True)
class ProjectPaths:
    """Caller-supplied recipe and external runtime directories."""

    project_root: Path
    runtime_root: Path

    def __post_init__(self) -> None:
        root = Path(self.project_root).expanduser().resolve()
        runtime = Path(self.runtime_root).expanduser().resolve()
        try:
            runtime.relative_to(root)
        except ValueError:
            pass
        else:
            message = "The runtime directory must be outside the shareable project."
            raise DataLensConfigurationError(message)
        object.__setattr__(self, "project_root", root)
        object.__setattr__(self, "runtime_root", runtime)


@dataclass(frozen=True)
class Deployment:
    """YC deployment identity; credentials are obtained only when opening a client."""

    organization_id: str
    target_path: str
    dashboard_name: str
    connection_name: str
    connection_id: str
    yc_profile: str
    yc_binary: str = "yc"

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass
class Session:
    paths: ProjectPaths
    deployment: Deployment
    runtime: dict[str, Any]
    client_factory: Callable[..., Any] | None = None
    reporter: Callable[..., Any] | None = None
    messages: list[str] = field(default_factory=list)

    @classmethod
    def load(
        cls,
        paths: ProjectPaths,
        deployment: Deployment,
        *,
        client_factory: Callable[..., Any] | None = None,
        reporter: Callable[..., Any] | None = None,
    ) -> Session:
        runtime = json.loads(
            (paths.project_root / "configs/runtime.json").read_text(encoding="utf-8")
        )
        if runtime.get("installation") != "yc":
            message = "This engine currently supports the YC installation."
            raise DataLensConfigurationError(message)
        interval = runtime.get("request_interval_seconds", 1.2)
        if (
            isinstance(interval, bool)
            or not isinstance(interval, (int, float))
            or not math.isfinite(interval)
            or not 0 <= interval <= 60  # noqa: PLR2004
        ):
            message = "request_interval_seconds must be a finite number between 0 and 60."
            raise DataLensConfigurationError(message)
        runtime.update(
            organization_id=deployment.organization_id,
            yc_profile=deployment.yc_profile,
            _request_count=0,
        )
        return cls(paths, deployment, runtime, client_factory, reporter)

    def emit(self, *values: Any, sep: Any = " ", end: Any = "\n", flush: Any = False) -> Any:
        self.messages.append(sep.join(str(value) for value in values))
        if self.reporter is not None:
            self.reporter(*values, sep=sep, end=end, flush=flush)

    @contextmanager
    def staged(self, root: Any) -> Iterator[Any]:
        """Validate proposed files in a temporary tree without patching imports."""
        child = replace(
            self, paths=ProjectPaths(root, self.paths.runtime_root), runtime=dict(self.runtime)
        )
        with bind_session(child):
            yield child


_active_session: ContextVar[Session | None] = ContextVar(
    "datalens_utils_active_session", default=None
)


def current_session() -> Session:
    value = _active_session.get()
    if value is None:
        message = (
            "Use a DataLensProject operation or its session() context before calling adapters."
        )
        raise DataLensConfigurationError(message)
    return value


@contextmanager
def bind_session(value: Session) -> Iterator[Session]:
    token = _active_session.set(value)
    try:
        yield value
    finally:
        _active_session.reset(token)
