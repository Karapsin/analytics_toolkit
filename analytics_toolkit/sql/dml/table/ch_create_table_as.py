from __future__ import annotations

from typing import Any

from ...backends.ch import create_table_as as _impl

InvalidSqlInputError = _impl.InvalidSqlInputError
UnsupportedConnectionTypeError = _impl.UnsupportedConnectionTypeError
get_connection_config = _impl.get_connection_config
get_sql_connection = _impl.get_sql_connection
get_ch_connection_for_host = _impl.get_ch_connection_for_host


def ch_create_table_as(*args: Any, **kwargs: Any) -> Any:
    original_get_connection_config = _impl.get_connection_config
    original_get_sql_connection = _impl.get_sql_connection
    original_get_ch_connection_for_host = _impl.get_ch_connection_for_host
    try:
        _impl.get_connection_config = get_connection_config
        _impl.get_sql_connection = get_sql_connection
        _impl.get_ch_connection_for_host = get_ch_connection_for_host
        return _impl.ch_create_table_as(*args, **kwargs)
    finally:
        _impl.get_connection_config = original_get_connection_config
        _impl.get_sql_connection = original_get_sql_connection
        _impl.get_ch_connection_for_host = original_get_ch_connection_for_host


__all__ = [
    "InvalidSqlInputError",
    "UnsupportedConnectionTypeError",
    "ch_create_table_as",
    "get_ch_connection_for_host",
    "get_connection_config",
    "get_sql_connection",
]
