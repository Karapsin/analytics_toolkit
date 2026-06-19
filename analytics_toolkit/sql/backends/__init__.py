from __future__ import annotations

from .base import BackendAdapter, BackendCapability, WriteMode
from .ch import (
    ClickHouseAdapter,
    ch_cluster_clause,
    format_ch_cluster_name,
    is_simple_identifier,
)
from .dbapi import DbApiBackendAdapter
from .gp import (
    GreenplumAdapter,
    format_gp_information_schema_type,
    split_gp_table_name,
)
from .registry import (
    BACKEND_ADAPTERS,
    BACKEND_ALIASES,
    BACKEND_REGISTRY,
    SUPPORTED_BACKENDS,
    UNSUPPORTED_BACKEND_MESSAGE,
    backend_capability_map,
    get_backend,
    get_backend_adapter,
    get_backend_capability,
    get_backend_names,
    normalize_backend_name,
    require_backend_name,
    supported_backend_message,
)
from .trino import TrinoAdapter, split_trino_table_name
from .utils import extract_row_count


__all__ = [
    "BACKEND_ADAPTERS",
    "BACKEND_ALIASES",
    "BACKEND_REGISTRY",
    "SUPPORTED_BACKENDS",
    "BackendAdapter",
    "BackendCapability",
    "ClickHouseAdapter",
    "DbApiBackendAdapter",
    "GreenplumAdapter",
    "TrinoAdapter",
    "UNSUPPORTED_BACKEND_MESSAGE",
    "WriteMode",
    "backend_capability_map",
    "ch_cluster_clause",
    "extract_row_count",
    "format_ch_cluster_name",
    "format_gp_information_schema_type",
    "get_backend",
    "get_backend_adapter",
    "get_backend_capability",
    "get_backend_names",
    "is_simple_identifier",
    "normalize_backend_name",
    "require_backend_name",
    "split_gp_table_name",
    "split_trino_table_name",
    "supported_backend_message",
]
