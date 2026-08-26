from __future__ import annotations

import os
from getpass import getpass

from .config import _iter_file_connection_values
from .references import (
    is_connection_value_reference,
    parse_connection_value_reference,
)


def set_missing_env_variables() -> list[str]:
    """Prompt for missing environment variables referenced by ``.connections``."""
    missing_names = _find_missing_environment_variable_names()
    pending_values: dict[str, str] = {}
    for name in missing_names:
        value = ""
        while not value:
            value = getpass(f"Enter value for environment variable '{name}': ")
        pending_values[name] = value

    os.environ.update(pending_values)
    return list(pending_values)


def _find_missing_environment_variable_names() -> list[str]:
    missing_names: list[str] = []
    seen_names: set[str] = set()
    for connection_key, raw_config in _iter_file_connection_values():
        for field_name, value in raw_config.items():
            if not is_connection_value_reference(value, field_name):
                continue
            source, source_key, _path = parse_connection_value_reference(
                connection_key,
                field_name,
                value,
            )
            if source != "env" or source_key in seen_names:
                continue
            seen_names.add(source_key)
            if not os.environ.get(source_key):
                missing_names.append(source_key)
    return missing_names


__all__ = ["set_missing_env_variables"]
