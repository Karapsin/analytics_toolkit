from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from .config import (
    ChConfig,
    GpConfig,
    TrinoConfig,
    get_connection_config,
    get_connections_file_path,
)
from .errors import SqlConfigError, UnsupportedConnectionTypeError
from ..execution.operation_runner import timed_public_sql_function
from analytics_toolkit.general import time_print


@timed_public_sql_function
def get_sql_connection(db_key: str) -> Any:
    config = get_connection_config(db_key)
    time_print(
        "Opening connection",
        connection=config.connection_key,
        backend=config.backend,
        phase="connect",
    )

    if isinstance(config, TrinoConfig):
        return _get_trino_connection(config)
    if isinstance(config, GpConfig):
        return _get_gp_connection(config)
    if isinstance(config, ChConfig):
        return _get_ch_connection(config)

    raise UnsupportedConnectionTypeError(
        "Unsupported connection type. Expected one of: 'trino', 'gp', 'ch'."
    )


def get_ch_connection_for_host(connection_key: str, host: str) -> Any:
    config = get_connection_config(connection_key)
    if not isinstance(config, ChConfig):
        raise UnsupportedConnectionTypeError(
            f"SQL connection '{config.connection_key}' is not a ClickHouse connection."
        )
    host_name = str(host).strip()
    if not host_name:
        raise ValueError("host must not be empty.")
    time_print(
        f"Opening connection to {host_name}",
        connection=config.connection_key,
        backend=config.backend,
        phase="connect",
    )
    return _get_ch_connection(replace(config, host=host_name))


@timed_public_sql_function
def _get_trino_connection(config: TrinoConfig) -> Any:
    try:
        import trino
        from trino.auth import BasicAuthentication
    except ImportError as exc:
        raise ImportError(
            "The 'trino' package is required for Trino connections."
        ) from exc

    ca_certs = _resolve_ca_certs(config.connection_key, config.ca_certs)
    verify_value = ca_certs or config.verify_value

    if config.auth_mode == "oauth2":
        auth = trino.auth.OAuth2Authentication()
    elif config.auth_mode == "basic":
        auth = BasicAuthentication(config.user, config.password) if config.password else None
    else:
        raise SqlConfigError(
            f"SQL connection '{config.connection_key}' has unsupported auth_mode. "
            "Expected 'basic' or 'oauth2'."
        )

    connect_kwargs = {
        "host": config.host,
        "port": config.port,
        "user": config.user,
        "http_scheme": config.http_scheme,
        "auth": auth,
        "verify": _parse_verify_value(verify_value),
    }
    if config.catalog:
        connect_kwargs["catalog"] = config.catalog
    if config.schema:
        connect_kwargs["schema"] = config.schema
    if config.request_timeout is not None:
        connect_kwargs["request_timeout"] = config.request_timeout
    if config.source:
        connect_kwargs["source"] = config.source

    return trino.dbapi.connect(**connect_kwargs)


def _get_gp_connection(config: GpConfig) -> Any:
    try:
        import psycopg2
    except ImportError as exc:
        raise ImportError(
            "The 'psycopg2' package is required for Greenplum connections."
        ) from exc

    connect_kwargs: dict[str, Any] = {
        "host": config.host,
        "port": config.port,
        "user": config.user,
        "password": config.password,
        "dbname": config.database,
        "connect_timeout": config.connect_timeout,
        "keepalives": int(config.keepalives),
    }
    if config.keepalives:
        connect_kwargs.update(
            {
                "keepalives_idle": config.keepalives_idle,
                "keepalives_interval": config.keepalives_interval,
                "keepalives_count": config.keepalives_count,
            }
        )
    if config.sslmode is not None:
        connect_kwargs["sslmode"] = config.sslmode
    ca_certs = _resolve_ca_certs(config.connection_key, config.ca_certs)
    if ca_certs is not None:
        connect_kwargs["sslrootcert"] = ca_certs
    if config.ssl_cert is not None:
        ssl_cert = _resolve_single_cert_path(
            config.connection_key,
            config.ssl_cert,
            field_name="ssl_cert",
        )
        connect_kwargs["sslcert"] = str(ssl_cert)
    if config.ssl_key is not None:
        ssl_key = _resolve_single_cert_path(
            config.connection_key,
            config.ssl_key,
            field_name="ssl_key",
        )
        connect_kwargs["sslkey"] = str(ssl_key)

    return psycopg2.connect(**connect_kwargs)


def _get_ch_connection(config: ChConfig) -> Any:
    try:
        import clickhouse_connect
        from clickhouse_connect import common as clickhouse_common
    except ImportError as exc:
        raise ImportError(
            "The 'clickhouse-connect' package is required for ClickHouse connections."
        ) from exc

    clickhouse_common.set_setting("autogenerate_session_id", False)

    client_kwargs = {
        "host": config.host,
        "port": config.port,
        "username": config.user,
        "password": config.password,
        "secure": config.secure,
    }
    if config.database:
        client_kwargs["database"] = config.database
    if config.verify_value is not None:
        client_kwargs["verify"] = _parse_verify_value(config.verify_value)
    ca_certs = _resolve_ch_ca_certs(config)
    if ca_certs is not None:
        client_kwargs["ca_cert"] = ca_certs
    if config.connect_timeout is not None:
        client_kwargs["connect_timeout"] = config.connect_timeout
    if config.send_receive_timeout is not None:
        client_kwargs["send_receive_timeout"] = config.send_receive_timeout
    if config.settings is not None:
        client_kwargs["settings"] = config.settings
    if config.interface is not None:
        client_kwargs["interface"] = config.interface
    if config.query_limit is not None:
        client_kwargs["query_limit"] = config.query_limit
    if config.query_retries is not None:
        client_kwargs["query_retries"] = config.query_retries
    if config.client_name is not None:
        client_kwargs["client_name"] = config.client_name

    return clickhouse_connect.get_client(**client_kwargs)


def _resolve_ch_ca_certs(config: ChConfig) -> str | None:
    if config.ca_certs:
        return _resolve_ca_certs(config.connection_key, config.ca_certs)
    if config.ca_certs_variable is None:
        return None

    try:
        from airflow.models.variable import Variable
    except ImportError as exc:
        raise SqlConfigError(
            f"SQL connection '{config.connection_key}' uses ca_certs_variable "
            "but Airflow Variable support is unavailable."
        ) from exc

    try:
        ca_certs = Variable.get(config.ca_certs_variable)
    except Exception as exc:
        raise SqlConfigError(
            f"Could not resolve Airflow Variable '{config.ca_certs_variable}' "
            f"for SQL connection '{config.connection_key}'. "
            f"{type(exc).__name__}: {exc}"
        ) from exc

    if not isinstance(ca_certs, str) or not ca_certs.strip():
        raise SqlConfigError(
            f"Airflow Variable '{config.ca_certs_variable}' for SQL connection "
            f"'{config.connection_key}' must be a non-empty string."
        )
    return _resolve_ca_certs(config.connection_key, [ca_certs.strip()])


def _parse_verify_value(value: str) -> bool | str:
    normalized = value.strip()
    lowered = normalized.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    return normalized


def _resolve_ca_certs(connection_key: str, ca_certs: list[str]) -> str | None:
    if not ca_certs:
        return None

    resolved_paths = [
        _resolve_single_cert_path(connection_key, cert_path, field_name="ca_certs")
        for cert_path in ca_certs
    ]
    if len(resolved_paths) == 1:
        return str(resolved_paths[0])

    connections_dir = get_connections_file_path().parent
    bundle_dir = connections_dir / ".certs" / ".generated"
    bundle_dir.mkdir(parents=True, exist_ok=True)
    bundle_path = bundle_dir / f"{_safe_file_key(connection_key)}-ca-bundle.pem"
    bundle_contents = "\n".join(
        path.read_text(encoding="utf-8").strip()
        for path in resolved_paths
    ) + "\n"
    if (
        not bundle_path.exists()
        or bundle_path.read_text(encoding="utf-8") != bundle_contents
    ):
        bundle_path.write_text(bundle_contents, encoding="utf-8")
    return str(bundle_path)


def _resolve_single_cert_path(
    connection_key: str,
    cert_path: str,
    *,
    field_name: str,
) -> Path:
    raw_path = cert_path.strip()
    path = Path(raw_path)
    if path.is_absolute():
        resolved = path
    else:
        connections_dir = get_connections_file_path().parent
        if "/" in raw_path or "\\" in raw_path:
            resolved = connections_dir / path
        else:
            resolved = connections_dir / ".certs" / path
    resolved = resolved.resolve()
    if not resolved.is_file():
        raise SqlConfigError(
            f"SQL connection '{connection_key}' field '{field_name}' references "
            f"missing certificate file: {resolved}"
        )
    return resolved


def _safe_file_key(connection_key: str) -> str:
    return "".join(
        character if character.isalnum() or character in {"-", "_"} else "_"
        for character in connection_key
    )
