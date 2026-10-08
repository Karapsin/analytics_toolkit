"""Dependency-free errors; public SDK exceptions retain their original types."""


class DataLensUtilsError(RuntimeError):
    """A project recipe, managed-resource, or reconciliation failure."""


class DataLensDependencyError(DataLensUtilsError):
    """The optional SDK runtime is missing or unsupported."""


class DataLensConfigurationError(DataLensUtilsError):
    """Project paths or deployment configuration are invalid."""
