from __future__ import annotations

from .._lazy_exports import lazy_export_dir, resolve_lazy_export


_EXPORTS = {
    "DbApiBackendAdapter": (
        "analytics_toolkit.sql.backends.dbapi",
        "DbApiBackendAdapter",
    ),
}

__all__ = ["DbApiBackendAdapter"]


def __getattr__(name: str):
    return resolve_lazy_export(name, _EXPORTS, __name__)


def __dir__() -> list[str]:
    return lazy_export_dir(globals(), _EXPORTS)
