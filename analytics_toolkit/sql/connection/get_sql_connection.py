from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from .config import ChConfig, get_connection_config, get_connections_file_path
from .errors import SqlConfigError, UnsupportedConnectionTypeError
from ..backends import get_backend
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

    return get_backend(config.backend).open_connection(
        config,
        parse_verify_value=_parse_verify_value,
        resolve_ca_certs=_resolve_ca_certs,
        resolve_single_cert_path=_resolve_single_cert_path_by_name,
        resolve_ch_ca_certs=_resolve_ch_ca_certs,
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
    return get_backend("ch").open_connection(
        replace(config, host=host_name),
        parse_verify_value=_parse_verify_value,
        resolve_ca_certs=_resolve_ca_certs,
        resolve_single_cert_path=_resolve_single_cert_path_by_name,
        resolve_ch_ca_certs=_resolve_ch_ca_certs,
    )


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


def _resolve_single_cert_path_by_name(
    connection_key: str,
    cert_path: str,
    field_name: str,
) -> Path:
    return _resolve_single_cert_path(
        connection_key,
        cert_path,
        field_name=field_name,
    )


def _safe_file_key(connection_key: str) -> str:
    return "".join(
        character if character.isalnum() or character in {"-", "_"} else "_"
        for character in connection_key
    )
