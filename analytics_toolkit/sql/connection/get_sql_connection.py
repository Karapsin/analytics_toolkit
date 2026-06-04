from __future__ import annotations

import os
import subprocess
from dataclasses import replace
from functools import wraps
from pathlib import Path
from typing import Any, Callable

from .config import (
    ChConfig,
    GpConfig,
    TrinoConfig,
    get_connection_config,
    get_connections_file_path,
)
from .errors import SqlConfigError, UnsupportedConnectionTypeError
from ..operation_runner import timed_public_sql_function
from analytics_toolkit.general import time_print


@timed_public_sql_function
def get_sql_connection(connection_key: str) -> Any:
    config = get_connection_config(connection_key)
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
def with_sql_connection(connection_key: str) -> Callable[..., Any]:
    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            config = get_connection_config(connection_key)
            connection = get_sql_connection(config.connection_key)
            try:
                return func(connection, *args, **kwargs)
            finally:
                time_print(
                    "Closing connection",
                    connection=config.connection_key,
                    backend=config.backend,
                    phase="close",
                )
                connection.close()

        return wrapper

    return decorator


def _get_trino_connection(config: TrinoConfig) -> Any:
    try:
        import trino
        from trino.auth import BasicAuthentication
    except ImportError as exc:
        raise ImportError(
            "The 'trino' package is required for Trino connections."
        ) from exc

    verify_value = config.verify_value
    if config.use_keychain_certs:
        verify_value = str(_build_trino_keychain_bundle(config))

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
    ca_cert = _resolve_ch_ca_cert(config)
    if ca_cert is not None:
        client_kwargs["ca_cert"] = ca_cert
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


def _resolve_ch_ca_cert(config: ChConfig) -> str | None:
    if config.ca_cert:
        return config.ca_cert
    if config.ca_cert_variable is None:
        return None

    try:
        from airflow.models.variable import Variable
    except ImportError as exc:
        raise SqlConfigError(
            f"SQL connection '{config.connection_key}' uses ca_cert_variable "
            "but Airflow Variable support is unavailable."
        ) from exc

    try:
        ca_cert = Variable.get(config.ca_cert_variable)
    except Exception as exc:
        raise SqlConfigError(
            f"Could not resolve Airflow Variable '{config.ca_cert_variable}' "
            f"for SQL connection '{config.connection_key}'. "
            f"{type(exc).__name__}: {exc}"
        ) from exc

    if not isinstance(ca_cert, str) or not ca_cert.strip():
        raise SqlConfigError(
            f"Airflow Variable '{config.ca_cert_variable}' for SQL connection "
            f"'{config.connection_key}' must be a non-empty string."
        )
    return ca_cert.strip()


def _parse_verify_value(value: str) -> bool | str:
    normalized = value.strip()
    lowered = normalized.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    return normalized


def _build_trino_keychain_bundle(config: TrinoConfig) -> Path:
    if not config.keychain_cert_names:
        raise SqlConfigError(
            f"SQL connection '{config.connection_key}' enables keychain certs "
            "but does not define keychain_cert_names."
        )

    certs_dir = _state_dir() / "certs"
    certs_dir.mkdir(parents=True, exist_ok=True)
    bundle_path = certs_dir / f"trino-{_safe_file_key(config.connection_key)}-keychain-ca.pem"
    keychains = [
        str(Path.home() / "Library/Keychains/login.keychain-db"),
        "/Library/Keychains/System.keychain",
    ]

    certificates: list[str] = []
    for cert_name in config.keychain_cert_names:
        certificate = _export_keychain_certificate(cert_name, keychains)
        if not certificate:
            raise SqlConfigError(
                f"Could not export '{cert_name}' from macOS Keychain."
            )
        certificates.append(certificate.strip())
    bundle_contents = "\n".join(certificates) + "\n"
    if not bundle_path.exists() or bundle_path.read_text(encoding="utf-8") != bundle_contents:
        bundle_path.write_text(bundle_contents, encoding="utf-8")
    return bundle_path


def _export_keychain_certificate(cert_name: str, keychains: list[str]) -> str:
    for keychain in keychains:
        if not Path(keychain).exists():
            continue

        result = subprocess.run(
            [
                "security",
                "find-certificate",
                "-a",
                "-c",
                cert_name,
                "-p",
                keychain,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0 and "BEGIN CERTIFICATE" in result.stdout:
            return result.stdout

    return ""


def _state_dir() -> Path:
    state_override = os.getenv("MAGNIT_UTILS_HOME")
    if state_override:
        return Path(state_override).expanduser()

    try:
        return get_connections_file_path().parent
    except SqlConfigError:
        return Path.cwd().resolve()


def _safe_file_key(connection_key: str) -> str:
    return "".join(
        character if character.isalnum() or character in {"-", "_"} else "_"
        for character in connection_key
    )
