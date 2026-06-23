from __future__ import annotations

from .._lazy_exports import lazy_export_dir, resolve_lazy_export


_EXPORTS = {
    "TrinoAdapter": ("analytics_toolkit.sql.backends.trino", "TrinoAdapter"),
    "split_trino_table_name": (
        "analytics_toolkit.sql.backends.trino",
        "split_trino_table_name",
    ),
}

__all__ = ["TrinoAdapter", "split_trino_table_name"]


def __getattr__(name: str):
    return resolve_lazy_export(name, _EXPORTS, __name__)


def __dir__() -> list[str]:
    return lazy_export_dir(globals(), _EXPORTS)
