"""Dependency-free errors; public SDK exceptions retain their original types."""

from __future__ import annotations

from typing import Any


class DataLensUtilsError(RuntimeError):
    """A project recipe, managed-resource, or reconciliation failure."""

    mismatches: list[dict[str, Any]]


class DataLensDependencyError(DataLensUtilsError):
    """The optional SDK runtime is missing or unsupported."""


class DataLensConfigurationError(DataLensUtilsError):
    """Project paths or deployment configuration are invalid."""


class DataLensCapabilityError(DataLensUtilsError):
    """A requested operation cannot be expressed through the public SDK."""

    def __init__(self, operation: str, report: dict[str, Any]) -> None:
        self.operation = operation
        self.report = report
        entry = report["operations"].get(operation, {})
        super().__init__(
            f"{operation} is unavailable for {report['installation']} with SDK "
            f"{report['sdk_version']}: {entry.get('reason', 'Unknown operation.')} "
            + " ".join(entry.get("requirements", ()))
        )
