from __future__ import annotations

import json
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Union, cast

from .errors import SqlConfigError, UnsupportedConnectionTypeError
from ..backends.registry import (
    BACKEND_REGISTRY,
    get_backend,
    normalize_backend_name as _registry_normalize_backend_name,
    require_backend_name,
)
from ..execution.operation_runner import timed_public_sql_function


BackendName = str
SUPPORTED_BACKENDS: set[str] = set(BACKEND_REGISTRY)
CONNECTIONS_FILE_NAME = ".connections"
DEFAULT_GP_CONNECT_TIMEOUT_SECONDS = 30
DEFAULT_GP_KEEPALIVES = True
DEFAULT_GP_KEEPALIVES_IDLE_SECONDS = 60
DEFAULT_GP_KEEPALIVES_INTERVAL_SECONDS = 10
DEFAULT_GP_KEEPALIVES_COUNT = 3
_MISSING_OVERRIDE = object()
_AIRFLOW_EXTRA_RESOLVER_KEYS = {"from", "key", "fallback"}


@dataclass(frozen=True)
class _AirflowConnectionEntry:
    connection_id: str
    backend: BackendName | None
    overrides: dict[str, Any]


@dataclass(frozen=True)
class _AirflowConnectionSource:
    connections: dict[str, _AirflowConnectionEntry]
    normalized_connections: dict[str, str]
    default_backend: BackendName | None


_AIRFLOW_CONNECTION_SOURCE: ContextVar[_AirflowConnectionSource | None] = (
    ContextVar("analytics_toolkit_sql_airflow_connection_source", default=None)
)


@dataclass(frozen=True)
class TrinoConfig:
    connection_key: str
    backend: BackendName
    host: str
    port: int
    user: str
    password: str | None
    catalog: str | None
    schema: str | None
    auth_mode: str
    http_scheme: str
    verify_value: str
    ca_certs: list[str]
    insert_chunk_size: int | None
    request_timeout: int | None
    source: str | None
    transfer_staging_schema: str | None
    transfer_staging_location: str | None
    upsert_partition_drop_sql_template: str | None = None


@dataclass(frozen=True)
class GpConfig:
    connection_key: str
    backend: BackendName
    host: str
    port: int
    user: str
    password: str
    database: str
    connect_timeout: int
    keepalives: bool
    keepalives_idle: int
    keepalives_interval: int
    keepalives_count: int
    sslmode: str | None
    ca_certs: list[str]
    ssl_cert: str | None
    ssl_key: str | None
    transfer_staging_schema: str | None


@dataclass(frozen=True)
class ChConfig:
    connection_key: str
    backend: BackendName
    host: str
    port: int
    user: str
    password: str
    database: str | None
    secure: bool
    verify_value: str | None
    ca_certs: list[str]
    ca_certs_variable: str | None
    connect_timeout: int | None
    send_receive_timeout: int | None
    settings: dict[str, Any] | None
    interface: str | None
    query_limit: int | None
    query_retries: int | None
    client_name: str | None
    transfer_staging_schema: str | None


ConnectionConfig = Union[TrinoConfig, GpConfig, ChConfig]


@dataclass(frozen=True)
class ConnectionValidationResult:
    connection_key: str
    backend: BackendName | None
    valid: bool
    connected: bool | None = None
    error: str | None = None


def get_connection_config(connection_key: str) -> ConnectionConfig:
    source = _AIRFLOW_CONNECTION_SOURCE.get()
    resolved_key = (
        _normalize_airflow_connection_id(connection_key)
        if source is not None
        else normalize_connection_key(connection_key)
    )
    raw_config = _get_raw_connection_config(resolved_key)
    return _build_connection_config(resolved_key, raw_config)


def airflow_connection_config(
    connection_id: str,
    backend: BackendName | str | None = None,
) -> ConnectionConfig:
    resolved_id = _normalize_airflow_connection_id(connection_id)
    raw_config = _get_airflow_raw_connection_config(
        resolved_id,
        _normalize_optional_backend_name(backend),
    )
    return _build_connection_config(resolved_id, raw_config)


@contextmanager
def use_airflow_connections(
    connection_backends: Mapping[str, BackendName | str] | None = None,
    *,
    default_backend: BackendName | str | None = None,
) -> Iterator[None]:
    connections: dict[str, _AirflowConnectionEntry] = {}
    normalized_connections: dict[str, str] = {}
    if connection_backends is not None:
        for connection_id, backend in connection_backends.items():
            resolved_id = _normalize_airflow_connection_id(connection_id)
            resolved_backend = _normalize_backend_name(backend)
            connections[resolved_id] = _AirflowConnectionEntry(
                connection_id=resolved_id,
                backend=resolved_backend,
                overrides={},
            )
            normalized_connections[normalize_connection_key(resolved_id)] = resolved_id

    source = _AirflowConnectionSource(
        connections=connections,
        normalized_connections=normalized_connections,
        default_backend=_normalize_optional_backend_name(default_backend),
    )
    token = _AIRFLOW_CONNECTION_SOURCE.set(source)
    try:
        yield
    finally:
        _AIRFLOW_CONNECTION_SOURCE.reset(token)


def _build_connection_config(
    connection_key: str,
    raw_config: dict[str, Any],
) -> ConnectionConfig:
    backend = _require_backend(connection_key, raw_config)
    return get_backend(backend).build_connection_config(connection_key, raw_config)


def get_connection_backend(connection_key: str) -> BackendName:
    config = get_connection_config(connection_key)
    return config.backend


def generate_dummy_connections(airflow: bool = False) -> Path:
    connections_path = Path.cwd() / CONNECTIONS_FILE_NAME
    if connections_path.exists():
        raise ValueError(f"SQL connections file already exists: {connections_path}")
    certs_dir = Path.cwd() / ".certs"

    content = (
        _build_dummy_airflow_connections()
        if airflow
        else _build_dummy_direct_connections()
    )
    certs_dir.mkdir(exist_ok=True)
    connections_path.write_text(
        json.dumps(content, indent=2) + "\n",
        encoding="utf-8",
    )
    _print_dummy_connections_cert_instructions(certs_dir)
    return connections_path


def _print_dummy_connections_cert_instructions(certs_dir: Path) -> None:
    print(f"Created {certs_dir} for local certificate files.")
    print(
        "Greenplum: put CA PEMs in .certs/ and set ca_certs; "
        "optional ssl_cert, ssl_key, sslmode."
    )
    print("Trino: put HTTPS CA PEMs in .certs/ and set ca_certs.")
    print(
        "ClickHouse: put HTTPS CA PEMs in .certs/ and set ca_certs; "
        "Airflow can use ca_certs_variable."
    )


def _build_dummy_direct_connections() -> dict[str, dict[str, Any]]:
    from ..backends.trino.config import example_upsert_partition_drop_sql_template

    return {
        "gp": {
            "type": "gp",
            "host": "gp.example",
            "port": 5432,
            "user": "user",
            "password": "password",
            "database": "db",
            "ca_certs": "gp-ca.pem",
        },
        "trino": {
            "type": "trino",
            "host": "trino.example",
            "port": 8080,
            "user": "user",
            "password": "password",
            "catalog": "iceberg",
            "schema": "sandbox",
            "http_scheme": "https",
            "ca_certs": "trino-ca.pem",
            "transfer_staging_schema": "object_storage.sandbox",
            "transfer_staging_location": "s3://bucket/tmp/analytics_toolkit_transfer",
            "upsert_partition_drop_sql_template": (
                example_upsert_partition_drop_sql_template()
            ),
        },
        "ch": {
            "type": "ch",
            "host": "ch.example",
            "port": 8123,
            "user": "user",
            "password": "password",
            "database": "default",
            "secure": True,
            "ca_certs": "clickhouse-ca.pem",
        },
    }


def _build_dummy_airflow_connections() -> dict[str, Any]:
    from ..backends.trino.config import example_upsert_partition_drop_sql_template

    return {
        "source": "airflow",
        "connections": {
            "gp": {"type": "gp", "ca_certs": "gp-ca.pem"},
            "trino": {
                "type": "trino",
                "ca_certs": "trino-ca.pem",
                "transfer_staging_schema": "object_storage.sandbox",
                "transfer_staging_location": "s3://bucket/tmp/analytics_toolkit_transfer",
                "upsert_partition_drop_sql_template": (
                    example_upsert_partition_drop_sql_template()
                ),
            },
            "ch": {"type": "ch", "ca_certs": "clickhouse-ca.pem"},
        },
    }


@timed_public_sql_function
def validate_connections(
    keys: Sequence[str] | None = None,
    *,
    connect: bool = False,
) -> list[ConnectionValidationResult]:
    if keys is None:
        normalized_keys = sorted(load_sql_connections())
    elif _AIRFLOW_CONNECTION_SOURCE.get() is not None:
        normalized_keys = [_normalize_airflow_connection_id(key) for key in keys]
    else:
        normalized_keys = [normalize_connection_key(key) for key in keys]

    results: list[ConnectionValidationResult] = []
    for key in normalized_keys:
        try:
            config = get_connection_config(key)
            connected: bool | None = None
            if connect:
                connection = _open_validation_connection(config.connection_key)
                try:
                    connected = True
                finally:
                    connection.close()
            results.append(
                ConnectionValidationResult(
                    connection_key=config.connection_key,
                    backend=config.backend,
                    valid=True,
                    connected=connected,
                )
            )
        except Exception as exc:
            results.append(
                ConnectionValidationResult(
                    connection_key=key,
                    backend=None,
                    valid=False,
                    connected=False if connect else None,
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
    return results


def _open_validation_connection(connection_key: str) -> Any:
    from .get_sql_connection import get_sql_connection

    return get_sql_connection(connection_key)


def resolve_connection_backend(connection_type_or_key: str) -> BackendName:
    normalized = normalize_connection_key(connection_type_or_key)
    if normalized in SUPPORTED_BACKENDS:
        return cast(BackendName, normalized)
    if _AIRFLOW_CONNECTION_SOURCE.get() is not None:
        return get_connection_backend(connection_type_or_key)
    return get_connection_backend(normalized)


def normalize_connection_key(connection_key: str) -> str:
    normalized = connection_key.strip().lower()
    if not normalized:
        raise SqlConfigError("Connection key must not be empty.")
    return normalized


def load_sql_connections() -> dict[str, dict[str, Any]]:
    source = _AIRFLOW_CONNECTION_SOURCE.get()
    if source is not None:
        if not source.connections:
            raise SqlConfigError(
                "Airflow connection mode cannot list all SQL connections. "
                "Pass keys to validate_connections or provide connection_backends "
                "to use_airflow_connections."
            )
        return {
            connection_key: _get_airflow_source_raw_connection_config(
                source,
                connection_key,
            )
            for connection_key in source.connections
        }

    return _load_file_sql_connections()


def _load_file_sql_connections() -> dict[str, dict[str, Any]]:
    connections_source = _load_file_connections_source()
    if isinstance(connections_source, _AirflowConnectionSource):
        return {
            connection_key: _get_airflow_source_raw_connection_config(
                connections_source,
                connection_key,
            )
            for connection_key in connections_source.connections
        }
    return connections_source


def _load_file_connections_source() -> (
    dict[str, dict[str, Any]] | _AirflowConnectionSource
):
    connections_path = get_connections_file_path()

    try:
        parsed = json.loads(connections_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SqlConfigError(
            f"{connections_path} must contain valid JSON."
        ) from exc

    if not isinstance(parsed, dict):
        raise SqlConfigError(f"{connections_path} must contain a JSON object.")

    if _is_airflow_connections_file(parsed, connections_path):
        return _parse_airflow_connections_file(parsed, connections_path)

    return _parse_direct_connections_file(parsed, connections_path)


def _is_airflow_connections_file(
    parsed: dict[str, Any],
    connections_path: Path,
) -> bool:
    if "source" not in parsed:
        return False

    source = parsed["source"]
    if isinstance(source, str):
        normalized_source = source.strip().lower()
        if normalized_source == "airflow":
            return True
        raise SqlConfigError(
            f"{connections_path} has unsupported SQL connection source "
            f"{source!r}. Expected 'airflow'."
        )

    if "connections" in parsed:
        raise SqlConfigError(
            f"{connections_path} field 'source' must be a string when "
            "'connections' is present."
        )
    return False


def _parse_direct_connections_file(
    parsed: dict[str, Any],
    connections_path: Path,
) -> dict[str, dict[str, Any]]:
    connections: dict[str, dict[str, Any]] = {}
    for raw_key, raw_config in parsed.items():
        if not isinstance(raw_key, str):
            raise SqlConfigError(f"{connections_path} keys must be strings.")
        normalized_key = normalize_connection_key(raw_key)
        if normalized_key in connections:
            raise SqlConfigError(
                f"Duplicate SQL connection key after normalization: {normalized_key}"
            )
        if not isinstance(raw_config, dict):
            raise SqlConfigError(
                f"{connections_path}['{normalized_key}'] must be a JSON object."
            )
        connections[normalized_key] = raw_config

    return connections


def _parse_airflow_connections_file(
    parsed: dict[str, Any],
    connections_path: Path,
) -> _AirflowConnectionSource:
    raw_connections = parsed.get("connections")
    if not isinstance(raw_connections, dict):
        raise SqlConfigError(
            f"{connections_path} field 'connections' must be a JSON object "
            "when source is 'airflow'."
        )

    connections: dict[str, _AirflowConnectionEntry] = {}
    normalized_connections: dict[str, str] = {}
    for raw_key, raw_config in raw_connections.items():
        if not isinstance(raw_key, str):
            raise SqlConfigError(
                f"{connections_path}['connections'] keys must be strings."
            )
        if not isinstance(raw_config, dict):
            raise SqlConfigError(
                f"{connections_path}['connections']['{raw_key}'] "
                "must be a JSON object."
            )

        normalized_key = normalize_connection_key(raw_key)
        if normalized_key in normalized_connections:
            raise SqlConfigError(
                "Duplicate SQL connection key after normalization: "
                f"{normalized_key}"
            )

        backend = _normalize_backend_name(
            _require_string(raw_config, raw_key, "type")
        )
        connection_id = _optional_string(
            raw_config,
            raw_key,
            "connection_id",
            raw_key,
        )
        if connection_id is None:
            connection_id = raw_key

        overrides = dict(raw_config)
        overrides.pop("type", None)
        overrides.pop("connection_id", None)
        connections[normalized_key] = _AirflowConnectionEntry(
            connection_id=_normalize_airflow_connection_id(connection_id),
            backend=backend,
            overrides=overrides,
        )
        normalized_connections[normalized_key] = normalized_key

    return _AirflowConnectionSource(
        connections=connections,
        normalized_connections=normalized_connections,
        default_backend=None,
    )


def get_connections_file_path() -> Path:
    connections_path = _find_connections_file_path()
    if connections_path is None:
        raise SqlConfigError(
            f"Missing SQL connections file: {CONNECTIONS_FILE_NAME}. "
            "Place it in the current working directory or one of its parents."
        )
    return connections_path


def _find_connections_file_path() -> Path | None:
    current_dir = Path.cwd().resolve()
    for directory in (current_dir, *current_dir.parents):
        connections_path = directory / CONNECTIONS_FILE_NAME
        if connections_path.is_file():
            return connections_path
    return None


def _get_raw_connection_config(connection_key: str) -> dict[str, Any]:
    source = _AIRFLOW_CONNECTION_SOURCE.get()
    if source is not None:
        return _get_airflow_source_raw_connection_config(
            source,
            connection_key,
            allow_dynamic=True,
        )

    connections_source = _load_file_connections_source()
    if isinstance(connections_source, _AirflowConnectionSource):
        return _get_airflow_source_raw_connection_config(
            connections_source,
            connection_key,
            allow_dynamic=False,
        )

    connections = connections_source
    try:
        return connections[connection_key]
    except KeyError as exc:
        available = ", ".join(sorted(connections)) or "<none>"
        raise UnsupportedConnectionTypeError(
            f"Unknown SQL connection key: {connection_key}. "
            f"Available keys: {available}"
        ) from exc


def _get_airflow_source_raw_connection_config(
    source: _AirflowConnectionSource,
    connection_key: str,
    *,
    allow_dynamic: bool = False,
) -> dict[str, Any]:
    entry = _get_airflow_source_entry(
        source,
        connection_key,
        allow_dynamic=allow_dynamic,
    )
    raw_config, extras = _get_airflow_raw_connection_config_and_extras(
        entry.connection_id,
        entry.backend,
    )
    raw_config.update(
        _resolve_airflow_entry_overrides(
            entry.overrides,
            extras,
            entry.connection_id,
        )
    )
    if entry.backend is not None:
        raw_config["type"] = entry.backend
    return raw_config


def _get_airflow_source_entry(
    source: _AirflowConnectionSource,
    connection_key: str,
    *,
    allow_dynamic: bool,
) -> _AirflowConnectionEntry:
    entry = source.connections.get(connection_key)
    if entry is not None:
        return entry

    normalized_key = normalize_connection_key(connection_key)
    resolved_key = source.normalized_connections.get(normalized_key)
    if resolved_key is not None:
        return source.connections[resolved_key]

    if allow_dynamic:
        return _AirflowConnectionEntry(
            connection_id=_normalize_airflow_connection_id(connection_key),
            backend=source.default_backend,
            overrides={},
        )

    available = ", ".join(sorted(source.connections)) or "<none>"
    raise UnsupportedConnectionTypeError(
        f"Unknown SQL connection key: {connection_key}. "
        f"Available keys: {available}"
    )


def _get_airflow_raw_connection_config(
    connection_id: str,
    backend: BackendName | None,
) -> dict[str, Any]:
    raw_config, _extras = _get_airflow_raw_connection_config_and_extras(
        connection_id,
        backend,
    )
    return raw_config


def _get_airflow_raw_connection_config_and_extras(
    connection_id: str,
    backend: BackendName | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    connection = _get_airflow_connection(connection_id)
    extras = _get_airflow_connection_extras(connection, connection_id)
    resolved_backend = backend or _resolve_airflow_connection_backend(
        connection,
        extras,
        connection_id,
    )

    raw_config: dict[str, Any] = {"type": resolved_backend}
    _set_if_not_none(raw_config, "host", getattr(connection, "host", None))
    _set_if_not_none(raw_config, "port", getattr(connection, "port", None))
    _set_if_not_none(raw_config, "user", getattr(connection, "login", None))
    _set_if_not_none(raw_config, "password", getattr(connection, "password", None))

    get_backend(resolved_backend).copy_airflow_fields(
        raw_config,
        extras,
        connection,
        _copy_extra_fields,
        _set_if_not_none,
    )

    return raw_config, extras


def _get_airflow_connection(connection_id: str) -> Any:
    try:
        from airflow.hooks.base import BaseHook
    except ImportError as exc:
        raise SqlConfigError(
            "Airflow connection resolution requires the 'apache-airflow' package."
        ) from exc

    try:
        return BaseHook.get_connection(connection_id)
    except Exception as exc:
        raise UnsupportedConnectionTypeError(
            f"Unknown Airflow connection ID: {connection_id}. "
            f"{type(exc).__name__}: {exc}"
        ) from exc


def _get_airflow_connection_extras(
    connection: Any,
    connection_id: str,
) -> dict[str, Any]:
    extra_dejson = getattr(connection, "extra_dejson", None)
    if extra_dejson is not None:
        if not isinstance(extra_dejson, dict):
            raise SqlConfigError(
                f"Airflow connection '{connection_id}' extra_dejson must be a dict."
            )
        return dict(extra_dejson)

    raw_extra = getattr(connection, "extra", None)
    if raw_extra is None or raw_extra == "":
        return {}
    if isinstance(raw_extra, dict):
        return dict(raw_extra)
    if isinstance(raw_extra, str):
        try:
            parsed = json.loads(raw_extra)
        except json.JSONDecodeError as exc:
            raise SqlConfigError(
                f"Airflow connection '{connection_id}' extra must contain valid JSON."
            ) from exc
        if not isinstance(parsed, dict):
            raise SqlConfigError(
                f"Airflow connection '{connection_id}' extra must contain a JSON object."
            )
        return parsed

    raise SqlConfigError(
        f"Airflow connection '{connection_id}' extra must be a JSON object."
    )


def _resolve_airflow_connection_backend(
    connection: Any,
    extras: dict[str, Any],
    connection_id: str,
) -> BackendName:
    raw_backend = (
        extras.get("type")
        or extras.get("backend")
        or getattr(connection, "conn_type", None)
    )
    if raw_backend is None:
        raise SqlConfigError(
            f"Airflow connection '{connection_id}' does not define a backend. "
            "Pass connection_backends/default_backend to use_airflow_connections "
            "or set connection type/extra 'type'."
        )
    return _normalize_backend_name(raw_backend)


def _normalize_backend_name(raw_backend: BackendName | str) -> BackendName:
    return _registry_normalize_backend_name(raw_backend)


def _normalize_optional_backend_name(
    raw_backend: BackendName | str | None,
) -> BackendName | None:
    if raw_backend is None:
        return None
    return _normalize_backend_name(raw_backend)


def _normalize_airflow_connection_id(connection_id: str) -> str:
    resolved_id = connection_id.strip()
    if not resolved_id:
        raise SqlConfigError("Airflow connection ID must not be empty.")
    return resolved_id


def _copy_extra_fields(
    raw_config: dict[str, Any],
    extras: dict[str, Any],
    field_names: Sequence[str],
) -> None:
    for field_name in field_names:
        if field_name in extras:
            raw_config[field_name] = extras[field_name]


def _resolve_airflow_entry_overrides(
    overrides: dict[str, Any],
    extras: dict[str, Any],
    connection_id: str,
) -> dict[str, Any]:
    resolved: dict[str, Any] = {}
    for field_name, value in overrides.items():
        if not _is_airflow_extra_resolver(field_name, value):
            resolved[field_name] = value
            continue

        resolved_value = _resolve_airflow_extra_resolver(
            field_name,
            value,
            extras,
            connection_id,
        )
        if resolved_value is not _MISSING_OVERRIDE:
            resolved[field_name] = resolved_value
    return resolved


def _is_airflow_extra_resolver(field_name: str, value: Any) -> bool:
    if not isinstance(value, dict) or "from" not in value:
        return False

    # ClickHouse settings are themselves a mapping. Treat settings as a resolver
    # only when it has the explicit resolver shape, so normal settings maps keep
    # working even if they contain a setting named "from".
    if field_name == "settings":
        return set(value).issubset(_AIRFLOW_EXTRA_RESOLVER_KEYS) or (
            "from" in value and "default" in value
        )
    return True


def _resolve_airflow_extra_resolver(
    field_name: str,
    resolver: dict[str, Any],
    extras: dict[str, Any],
    connection_id: str,
) -> Any:
    unexpected_keys = set(resolver) - _AIRFLOW_EXTRA_RESOLVER_KEYS
    if unexpected_keys:
        unexpected = ", ".join(sorted(unexpected_keys))
        raise SqlConfigError(
            f"Airflow connection '{connection_id}' override '{field_name}' "
            f"has unsupported resolver field(s): {unexpected}."
        )

    raw_source = resolver.get("from")
    if not isinstance(raw_source, str) or raw_source.strip().lower() != "extra":
        raise SqlConfigError(
            f"Airflow connection '{connection_id}' override '{field_name}' "
            "resolver field 'from' must be 'extra'."
        )

    raw_key = resolver.get("key", field_name)
    if not isinstance(raw_key, str) or not raw_key.strip():
        raise SqlConfigError(
            f"Airflow connection '{connection_id}' override '{field_name}' "
            "resolver field 'key' must be a non-empty string."
        )

    extra_key = raw_key.strip()
    if extra_key in extras and extras[extra_key] is not None:
        return extras[extra_key]
    if "fallback" in resolver:
        return resolver["fallback"]
    return _MISSING_OVERRIDE


def _set_if_not_none(
    raw_config: dict[str, Any],
    field_name: str,
    value: Any,
) -> None:
    if value is not None:
        raw_config[field_name] = value


def _require_backend(connection_key: str, config: dict[str, Any]) -> BackendName:
    raw_backend = _require_string(config, connection_key, "type")
    return require_backend_name(raw_backend, connection_key=connection_key)


def _require_string(
    config: dict[str, Any],
    connection_key: str,
    field_name: str,
) -> str:
    value = _optional_string(config, connection_key, field_name)
    if value is None:
        raise SqlConfigError(
            f"SQL connection '{connection_key}' is missing required field: {field_name}"
        )
    return value


def _optional_string(
    config: dict[str, Any],
    connection_key: str,
    field_name: str,
    default: str | None = None,
) -> str | None:
    if field_name not in config:
        return default

    value = config[field_name]
    if value is None:
        return default
    if not isinstance(value, str):
        raise SqlConfigError(
            f"SQL connection '{connection_key}' field '{field_name}' must be a string."
        )

    normalized = value.strip()
    return normalized if normalized else default


def _optional_int(
    config: dict[str, Any],
    connection_key: str,
    field_name: str,
    default: int,
) -> int:
    if field_name not in config or config[field_name] is None:
        return default

    value = config[field_name]
    if isinstance(value, bool):
        raise SqlConfigError(
            f"SQL connection '{connection_key}' field '{field_name}' must be an integer."
        )
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = int(value.strip())
        except ValueError as exc:
            raise SqlConfigError(
                f"SQL connection '{connection_key}' field '{field_name}' must be an integer."
            ) from exc
    else:
        raise SqlConfigError(
            f"SQL connection '{connection_key}' field '{field_name}' must be an integer."
        )

    if parsed <= 0:
        raise SqlConfigError(
            f"SQL connection '{connection_key}' field '{field_name}' must be positive."
        )
    return parsed


def _optional_positive_int(
    config: dict[str, Any],
    connection_key: str,
    field_name: str,
) -> int | None:
    if field_name not in config or config[field_name] is None:
        return None

    return _optional_int(config, connection_key, field_name, 1)


def _optional_non_negative_int(
    config: dict[str, Any],
    connection_key: str,
    field_name: str,
) -> int | None:
    if field_name not in config or config[field_name] is None:
        return None

    value = config[field_name]
    if isinstance(value, bool):
        raise SqlConfigError(
            f"SQL connection '{connection_key}' field '{field_name}' must be an integer."
        )
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = int(value.strip())
        except ValueError as exc:
            raise SqlConfigError(
                f"SQL connection '{connection_key}' field '{field_name}' must be an integer."
            ) from exc
    else:
        raise SqlConfigError(
            f"SQL connection '{connection_key}' field '{field_name}' must be an integer."
        )

    if parsed < 0:
        raise SqlConfigError(
            f"SQL connection '{connection_key}' field '{field_name}' "
            "must be non-negative."
        )
    return parsed


def _optional_bool(
    config: dict[str, Any],
    connection_key: str,
    field_name: str,
    default: bool,
) -> bool:
    if field_name not in config or config[field_name] is None:
        return default

    value = config[field_name]
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes"}:
            return True
        if normalized in {"false", "0", "no"}:
            return False

    raise SqlConfigError(
        f"SQL connection '{connection_key}' field '{field_name}' must be a boolean."
    )


def _optional_bool_or_string_as_string(
    config: dict[str, Any],
    connection_key: str,
    field_name: str,
) -> str | None:
    if field_name not in config or config[field_name] is None:
        return None

    value = config[field_name]
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, str):
        normalized = value.strip()
        return normalized if normalized else None

    raise SqlConfigError(
        f"SQL connection '{connection_key}' field '{field_name}' "
        "must be a boolean or string."
    )


def _optional_mapping(
    config: dict[str, Any],
    connection_key: str,
    field_name: str,
) -> dict[str, Any] | None:
    value = config.get(field_name)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise SqlConfigError(
            f"SQL connection '{connection_key}' field '{field_name}' "
            "must be a JSON object."
        )

    for key in value:
        if not isinstance(key, str):
            raise SqlConfigError(
                f"SQL connection '{connection_key}' field '{field_name}' "
                "must contain only string keys."
            )
    return dict(value)


def _reject_removed_fields(
    config: dict[str, Any],
    connection_key: str,
    field_names: Sequence[str],
) -> None:
    for field_name in field_names:
        if field_name in config:
            raise SqlConfigError(
                f"SQL connection '{connection_key}' field '{field_name}' "
                "is not supported. "
                "Use 'ca_certs' for CA certificate files."
            )


def _optional_string_or_string_list(
    config: dict[str, Any],
    connection_key: str,
    field_name: str,
) -> list[str]:
    value = config.get(field_name)
    if value is None:
        return []
    if isinstance(value, str):
        names = [value.strip()]
    elif isinstance(value, list):
        names = []
        for item in value:
            if not isinstance(item, str):
                raise SqlConfigError(
                    f"SQL connection '{connection_key}' field '{field_name}' "
                    "must contain only strings."
                )
            names.append(item.strip())
    else:
        raise SqlConfigError(
            f"SQL connection '{connection_key}' field '{field_name}' "
            "must be a string or list of strings."
        )

    return [name for name in names if name]
