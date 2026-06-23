from __future__ import annotations

from .._lazy_exports import lazy_export_dir, resolve_lazy_export


_EXPORTS = {
    "GreenplumAdapter": ("analytics_toolkit.sql.backends.gp", "GreenplumAdapter"),
    "format_gp_information_schema_type": (
        "analytics_toolkit.sql.backends.gp",
        "format_gp_information_schema_type",
    ),
    "split_gp_table_name": ("analytics_toolkit.sql.backends.gp", "split_gp_table_name"),
}

__all__ = [
    "GreenplumAdapter",
    "format_gp_information_schema_type",
    "split_gp_table_name",
]


def __getattr__(name: str):
    return resolve_lazy_export(name, _EXPORTS, __name__)


def __dir__() -> list[str]:
    return lazy_export_dir(globals(), _EXPORTS)
