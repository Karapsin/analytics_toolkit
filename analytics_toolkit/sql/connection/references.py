from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from importlib import import_module
from typing import TYPE_CHECKING, Any, cast

from .errors import SqlConfigError
from .secret_file import load_secret_values

if TYPE_CHECKING:
    from pathlib import Path

_REFERENCE_FIELDS = frozenset({"from", "key", "path"})
_REFERENCE_SOURCES = frozenset({".secrets", "airflow_variable", "env"})
_LITERAL_MAPPING_FIELDS = frozenset({"ddl_defaults", "settings"})
_LITERAL_ROUTING_FIELDS = frozenset({"connection_id", "type"})
_SECRET_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def is_connection_value_reference(value: Any, field_name: str) -> bool:
    if not isinstance(value, dict) or "from" not in value:
        return False
    if field_name in _LITERAL_MAPPING_FIELDS:
        return set(value).issubset(_REFERENCE_FIELDS)
    return True


def resolve_connection_value_references(
    connection_key: str,
    raw_config: dict[str, Any],
) -> dict[str, Any]:
    resolved = dict(raw_config)
    secret_path: Path | None = None
    secret_values: dict[str, str] | None = None
    for field_name, value in raw_config.items():
        if not is_connection_value_reference(value, field_name):
            continue
        source, source_key, path = parse_connection_value_reference(
            connection_key,
            field_name,
            value,
        )
        if source == ".secrets" and secret_values is None:
            try:
                secret_path, secret_values = load_secret_values()
            except SqlConfigError as exc:
                message = (
                    f"Could not resolve .secrets for SQL connection "
                    f"'{connection_key}' field '{field_name}': {exc}"
                )
                raise SqlConfigError(message) from exc
        resolved[field_name] = _resolve_reference(
            connection_key,
            field_name,
            (source, source_key, path),
            (secret_path, secret_values) if secret_values is not None else None,
        )
    return resolved


def _resolve_reference(
    connection_key: str,
    field_name: str,
    parsed_reference: tuple[str, str, tuple[str, ...] | None],
    secret_source: tuple[Path | None, dict[str, str]] | None,
) -> Any:
    source, source_key, path = parsed_reference
    value = _read_source_value(
        connection_key,
        field_name,
        (source, source_key),
        secret_source,
    )

    if path is None:
        return value
    document = _parse_json_value(
        connection_key,
        field_name,
        source,
        source_key,
        value,
    )
    return _read_json_path(
        connection_key,
        field_name,
        document,
        path,
    )


def parse_connection_value_reference(
    connection_key: str,
    field_name: str,
    reference: dict[str, Any],
) -> tuple[str, str, tuple[str, ...] | None]:
    if field_name in _LITERAL_ROUTING_FIELDS:
        message = (
            f"SQL connection '{connection_key}' routing field '{field_name}' "
            "must be a literal value."
        )
        raise SqlConfigError(message)
    return _parse_reference(connection_key, field_name, reference)


def _parse_reference(
    connection_key: str,
    field_name: str,
    reference: dict[str, Any],
) -> tuple[str, str, tuple[str, ...] | None]:
    unexpected_fields = set(reference) - _REFERENCE_FIELDS
    if unexpected_fields:
        unexpected = ", ".join(sorted(unexpected_fields))
        message = (
            f"SQL connection '{connection_key}' field '{field_name}' reference "
            f"has unsupported field(s): {unexpected}."
        )
        raise SqlConfigError(message)

    raw_source = reference.get("from")
    if not isinstance(raw_source, str):
        message = (
            f"SQL connection '{connection_key}' field '{field_name}' reference "
            "field 'from' must be '.secrets', 'env', or 'airflow_variable'."
        )
        raise SqlConfigError(message)
    source = raw_source.strip().lower()
    if source not in _REFERENCE_SOURCES:
        message = (
            f"SQL connection '{connection_key}' field '{field_name}' reference "
            "field 'from' must be '.secrets', 'env', or 'airflow_variable'."
        )
        raise SqlConfigError(message)

    raw_source_key = reference.get("key")
    if not isinstance(raw_source_key, str) or not raw_source_key.strip():
        message = (
            f"SQL connection '{connection_key}' field '{field_name}' reference "
            "field 'key' must be a non-empty string."
        )
        raise SqlConfigError(message)
    source_key = raw_source_key.strip()
    if source == ".secrets" and _SECRET_KEY_RE.fullmatch(source_key) is None:
        message = (
            f"SQL connection '{connection_key}' field '{field_name}' reference "
            "to '.secrets' must use a shell identifier as field 'key'."
        )
        raise SqlConfigError(message)
    path = (
        _parse_path(connection_key, field_name, reference["path"]) if "path" in reference else None
    )
    return source, source_key, path


def _read_source_value(
    connection_key: str,
    field_name: str,
    source_reference: tuple[str, str],
    secret_source: tuple[Path | None, dict[str, str]] | None,
) -> Any:
    source, source_key = source_reference
    if source == "env":
        try:
            return os.environ[source_key]
        except KeyError as exc:
            message = (
                f"Environment variable '{source_key}' referenced by SQL connection "
                f"'{connection_key}' field '{field_name}' is not set."
            )
            raise SqlConfigError(message) from exc
    if source == ".secrets":
        secret_path, secret_values = cast(
            "tuple[Path | None, dict[str, str]]",
            secret_source,
        )
        try:
            value = secret_values[source_key]
        except KeyError as exc:
            message = (
                f"Secret '{source_key}' referenced by SQL connection "
                f"'{connection_key}' field '{field_name}' is not set in {secret_path}."
            )
            raise SqlConfigError(message) from exc
        if not value:
            message = (
                f"Secret '{source_key}' referenced by SQL connection "
                f"'{connection_key}' field '{field_name}' is empty in {secret_path}."
            )
            raise SqlConfigError(message)
        return value
    return _read_airflow_variable(connection_key, field_name, source_key)


def _read_airflow_variable(
    connection_key: str,
    field_name: str,
    variable_key: str,
) -> Any:
    try:
        variable_module = import_module("airflow.models.variable")
    except ImportError as exc:
        message = (
            f"SQL connection '{connection_key}' field '{field_name}' references "
            f"Airflow Variable '{variable_key}', but Airflow Variable support is unavailable."
        )
        raise SqlConfigError(message) from exc

    try:
        return variable_module.Variable.get(variable_key)
    except Exception as exc:
        message = (
            f"Could not resolve Airflow Variable '{variable_key}' for SQL connection "
            f"'{connection_key}' field '{field_name}' ({type(exc).__name__})."
        )
        raise SqlConfigError(message) from exc


def _parse_path(
    connection_key: str,
    field_name: str,
    raw_path: Any,
) -> tuple[str, ...]:
    if not isinstance(raw_path, list) or any(
        not isinstance(part, str) or not part for part in raw_path
    ):
        message = (
            f"SQL connection '{connection_key}' field '{field_name}' reference "
            "field 'path' must be an array of non-empty strings."
        )
        raise SqlConfigError(message)
    return tuple(raw_path)


def _parse_json_value(
    connection_key: str,
    field_name: str,
    source: str,
    source_key: str,
    value: Any,
) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        message = (
            f"SQL connection '{connection_key}' field '{field_name}' reference to "
            f"{source} '{source_key}' must contain valid JSON when 'path' is used."
        )
        raise SqlConfigError(message) from exc


def _read_json_path(
    connection_key: str,
    field_name: str,
    document: Any,
    path: tuple[str, ...],
) -> Any:
    value = document
    for part in path:
        if not isinstance(value, Mapping) or part not in value:
            message = (
                f"SQL connection '{connection_key}' field '{field_name}' reference "
                f"path {list(path)!r} was not found."
            )
            raise SqlConfigError(message)
        value = value[part]
    return value


__all__ = [
    "is_connection_value_reference",
    "parse_connection_value_reference",
    "resolve_connection_value_references",
]
