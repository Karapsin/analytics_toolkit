from __future__ import annotations

from .._lazy_exports import lazy_export_dir, resolve_lazy_export


_EXPORTS = {
    "BackendAdapter": ("analytics_toolkit.sql.backends.base", "BackendAdapter"),
    "BackendCapability": ("analytics_toolkit.sql.backends.base", "BackendCapability"),
    "UNSUPPORTED_BACKEND_MESSAGE": (
        "analytics_toolkit.sql.backends.registry",
        "UNSUPPORTED_BACKEND_MESSAGE",
    ),
    "WriteMode": ("analytics_toolkit.sql.backends.base", "WriteMode"),
}

__all__ = [
    "BackendAdapter",
    "BackendCapability",
    "UNSUPPORTED_BACKEND_MESSAGE",
    "WriteMode",
]


def __getattr__(name: str):
    return resolve_lazy_export(name, _EXPORTS, __name__)


def __dir__() -> list[str]:
    return lazy_export_dir(globals(), _EXPORTS)
