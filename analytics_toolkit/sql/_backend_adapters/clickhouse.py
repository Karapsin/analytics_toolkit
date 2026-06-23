from __future__ import annotations

from .._lazy_exports import lazy_export_dir, resolve_lazy_export


_EXPORTS = {
    "ClickHouseAdapter": ("analytics_toolkit.sql.backends.ch", "ClickHouseAdapter"),
    "ch_cluster_clause": ("analytics_toolkit.sql.backends.ch", "ch_cluster_clause"),
    "format_ch_cluster_name": (
        "analytics_toolkit.sql.backends.ch",
        "format_ch_cluster_name",
    ),
    "is_simple_identifier": (
        "analytics_toolkit.sql.backends.ch",
        "is_simple_identifier",
    ),
}

__all__ = [
    "ClickHouseAdapter",
    "ch_cluster_clause",
    "format_ch_cluster_name",
    "is_simple_identifier",
]


def __getattr__(name: str):
    return resolve_lazy_export(name, _EXPORTS, __name__)


def __dir__() -> list[str]:
    return lazy_export_dir(globals(), _EXPORTS)
