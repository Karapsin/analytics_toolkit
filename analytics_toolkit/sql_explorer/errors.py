from __future__ import annotations


class SqlExplorerError(RuntimeError):
    """Base error raised by the SQL explorer."""


class SqlExplorerDependencyError(SqlExplorerError):
    """Raised when optional terminal dependencies are unavailable."""


class SqlExplorerEnvironmentError(SqlExplorerError):
    """Raised when the explorer is not attached to an interactive terminal."""


class SqlExplorerConfigurationError(SqlExplorerError):
    """Raised when an explorer database or preference is invalid."""


__all__ = [
    "SqlExplorerConfigurationError",
    "SqlExplorerDependencyError",
    "SqlExplorerEnvironmentError",
    "SqlExplorerError",
]
