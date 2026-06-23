from __future__ import annotations

from .._lazy_exports import lazy_export_dir, resolve_lazy_export


_EXPORTS = {
    "BACKEND_ADAPTERS": ("analytics_toolkit.sql.backends", "BACKEND_ADAPTERS"),
    "BackendAdapter": ("analytics_toolkit.sql.backends", "BackendAdapter"),
    "BackendCapability": ("analytics_toolkit.sql.backends", "BackendCapability"),
    "ClickHouseAdapter": ("analytics_toolkit.sql.backends", "ClickHouseAdapter"),
    "DbApiBackendAdapter": ("analytics_toolkit.sql.backends", "DbApiBackendAdapter"),
    "GreenplumAdapter": ("analytics_toolkit.sql.backends", "GreenplumAdapter"),
    "TrinoAdapter": ("analytics_toolkit.sql.backends", "TrinoAdapter"),
    "UNSUPPORTED_BACKEND_MESSAGE": (
        "analytics_toolkit.sql.backends",
        "UNSUPPORTED_BACKEND_MESSAGE",
    ),
    "WriteMode": ("analytics_toolkit.sql.backends", "WriteMode"),
    "ch_cluster_clause": ("analytics_toolkit.sql.backends", "ch_cluster_clause"),
    "extract_row_count": ("analytics_toolkit.sql.backends", "extract_row_count"),
    "format_ch_cluster_name": (
        "analytics_toolkit.sql.backends",
        "format_ch_cluster_name",
    ),
    "format_gp_information_schema_type": (
        "analytics_toolkit.sql.backends",
        "format_gp_information_schema_type",
    ),
    "get_backend_adapter": ("analytics_toolkit.sql.backends", "get_backend_adapter"),
    "is_simple_identifier": ("analytics_toolkit.sql.backends", "is_simple_identifier"),
    "split_gp_table_name": ("analytics_toolkit.sql.backends", "split_gp_table_name"),
    "split_trino_table_name": (
        "analytics_toolkit.sql.backends",
        "split_trino_table_name",
    ),
}


__all__ = [
    "BACKEND_ADAPTERS",
    "BackendAdapter",
    "BackendCapability",
    "ClickHouseAdapter",
    "DbApiBackendAdapter",
    "GreenplumAdapter",
    "TrinoAdapter",
    "UNSUPPORTED_BACKEND_MESSAGE",
    "WriteMode",
    "ch_cluster_clause",
    "extract_row_count",
    "format_ch_cluster_name",
    "format_gp_information_schema_type",
    "get_backend_adapter",
    "is_simple_identifier",
    "split_gp_table_name",
    "split_trino_table_name",
]


def __getattr__(name: str):
    return resolve_lazy_export(name, _EXPORTS, __name__)


def __dir__() -> list[str]:
    return lazy_export_dir(globals(), _EXPORTS)
