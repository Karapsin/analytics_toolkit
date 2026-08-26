from __future__ import annotations

from getpass import getpass

from .config import _iter_file_connection_values
from .references import (
    is_connection_value_reference,
    parse_connection_value_reference,
)
from .secret_file import (
    SECRETS_FILE_NAME,
    _quote_secret_value,
    _read_secret_file,
    _write_secret_file,
)


def set_missing_secrets() -> list[str]:
    """Prompt for missing ``.secrets`` values referenced by ``.connections``."""
    referenced_names = _find_referenced_secret_names()
    if not referenced_names:
        return []

    secret_file = _read_secret_file(required=False)
    missing_names = [name for name in referenced_names if not secret_file.values.get(name)]
    pending_values: dict[str, str] = {}
    for name in missing_names:
        value = ""
        while not value:
            value = getpass(f"Enter value for secret '{name}': ")
        _quote_secret_value(name, value)
        pending_values[name] = value

    if pending_values:
        _write_secret_file(secret_file, pending_values)
    return list(pending_values)


def _find_referenced_secret_names() -> list[str]:
    secret_names: list[str] = []
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
            if source != SECRETS_FILE_NAME or source_key in seen_names:
                continue
            seen_names.add(source_key)
            secret_names.append(source_key)
    return secret_names


__all__ = ["set_missing_secrets"]
