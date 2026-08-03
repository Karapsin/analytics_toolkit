from __future__ import annotations

from typing import Any, Callable, Literal

AIRFLOW_EXTRA_FIELDS = (
    "driver",
    "secure",
    "verify",
    "ca_certs",
    "transfer_staging_schema",
    "ca_certs_variable",
    "connect_timeout",
    "send_receive_timeout",
    "settings",
    "interface",
    "query_limit",
    "query_retries",
    "client_name",
    "compression",
    "ddl_defaults",
)


def build_config(connection_key: str, raw_config: dict[str, Any]) -> Any:
    from analytics_toolkit.sql.connection.ddl_defaults import (  # noqa: PLC0415
        parse_ddl_defaults,
    )
    from analytics_toolkit.sql.connection.errors import SqlConfigError  # noqa: PLC0415

    from ...connection.config import (
        ChConfig,
        _optional_bool,
        _optional_bool_or_string_as_string,
        _optional_int,
        _optional_mapping,
        _optional_non_negative_int,
        _optional_positive_int,
        _optional_string,
        _optional_string_or_string_list,
        _reject_removed_fields,
        _require_string,
    )

    _reject_removed_fields(
        raw_config,
        connection_key,
        ["ca_cert", "ca_cert_variable"],
    )
    driver = _parse_driver(raw_config, connection_key, SqlConfigError)
    if driver == "native":
        incompatible = [
            field for field in ("interface", "query_limit", "query_retries") if field in raw_config
        ]
        if incompatible:
            fields = ", ".join(repr(field) for field in incompatible)
            message = (
                f"SQL connection '{connection_key}' uses driver 'native', but "
                f"HTTP-only field(s) {fields} were supplied."
            )
            raise SqlConfigError(message)
    ca_certs = _optional_string_or_string_list(
        raw_config,
        connection_key,
        "ca_certs",
    )
    ca_certs_variable = _optional_string(
        raw_config,
        connection_key,
        "ca_certs_variable",
    )
    if ca_certs:
        ca_certs_variable = None
    return ChConfig(
        connection_key=connection_key,
        backend="ch",
        driver=driver,
        host=_require_string(raw_config, connection_key, "host"),
        port=_optional_int(
            raw_config,
            connection_key,
            "port",
            9000 if driver == "native" else 8123,
        ),
        user=_require_string(raw_config, connection_key, "user"),
        password=_require_string(raw_config, connection_key, "password"),
        database=_optional_string(raw_config, connection_key, "database"),
        secure=_optional_bool(raw_config, connection_key, "secure", False),
        verify_value=_optional_bool_or_string_as_string(
            raw_config,
            connection_key,
            "verify",
        ),
        ca_certs=ca_certs,
        ca_certs_variable=ca_certs_variable,
        connect_timeout=_optional_positive_int(
            raw_config,
            connection_key,
            "connect_timeout",
        ),
        send_receive_timeout=_optional_positive_int(
            raw_config,
            connection_key,
            "send_receive_timeout",
        ),
        settings=_optional_mapping(raw_config, connection_key, "settings"),
        interface=_optional_string(raw_config, connection_key, "interface"),
        query_limit=_optional_non_negative_int(
            raw_config,
            connection_key,
            "query_limit",
        ),
        query_retries=_optional_non_negative_int(
            raw_config,
            connection_key,
            "query_retries",
        ),
        client_name=_optional_string(raw_config, connection_key, "client_name"),
        compression=_parse_compression(raw_config, connection_key, SqlConfigError),
        transfer_staging_schema=_optional_string(
            raw_config,
            connection_key,
            "transfer_staging_schema",
        ),
        ddl_defaults=parse_ddl_defaults(raw_config.get("ddl_defaults"), connection_key, "ch"),
    )


def _parse_driver(
    raw_config: dict[str, Any],
    connection_key: str,
    error_type: type[Exception],
) -> Literal["http", "native"]:
    value = raw_config.get("driver", "http")
    if isinstance(value, str) and value.strip().lower() in {"http", "native"}:
        return "native" if value.strip().lower() == "native" else "http"
    message = f"SQL connection '{connection_key}' field 'driver' must be 'http' or 'native'."
    raise error_type(message)


def _parse_compression(
    raw_config: dict[str, Any],
    connection_key: str,
    error_type: type[Exception],
) -> bool | Literal["lz4", "zstd"]:
    value = raw_config.get("compression", False)
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in {"lz4", "zstd"}:
        return "zstd" if value.strip().lower() == "zstd" else "lz4"
    message = (
        f"SQL connection '{connection_key}' field 'compression' must be a boolean, "
        "'lz4', or 'zstd'."
    )
    raise error_type(message)


def open_connection(
    config: Any,
    *,
    parse_verify_value: Callable[[str], bool | str],
    resolve_ch_ca_certs: Callable[[Any], str | None],
) -> Any:
    if getattr(config, "driver", "http") == "native":
        return _open_native_connection(
            config,
            parse_verify_value=parse_verify_value,
            resolve_ch_ca_certs=resolve_ch_ca_certs,
        )
    return _open_http_connection(
        config,
        parse_verify_value=parse_verify_value,
        resolve_ch_ca_certs=resolve_ch_ca_certs,
    )


def _open_http_connection(
    config: Any,
    *,
    parse_verify_value: Callable[[str], bool | str],
    resolve_ch_ca_certs: Callable[[Any], str | None],
) -> Any:
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
        client_kwargs["verify"] = parse_verify_value(config.verify_value)
    ca_certs = resolve_ch_ca_certs(config)
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


def _open_native_connection(
    config: Any,
    *,
    parse_verify_value: Callable[[str], bool | str],
    resolve_ch_ca_certs: Callable[[Any], str | None],
) -> Any:
    try:
        from clickhouse_driver import Client  # noqa: PLC0415
    except ImportError as exc:
        message = (
            "Native ClickHouse connections require the 'clickhouse-driver' package.\n"
            "Install analytics-toolkit[clickhouse-native]."
        )
        raise ImportError(message) from exc

    from .native_client import NativeClickHouseClient  # noqa: PLC0415

    client_kwargs: dict[str, Any] = {
        "host": config.host,
        "port": config.port,
        "user": config.user,
        "password": config.password,
        "database": config.database or "",
        "secure": config.secure,
        "compression": config.compression,
    }
    if config.verify_value is not None:
        client_kwargs["verify"] = parse_verify_value(config.verify_value)
    ca_certs = resolve_ch_ca_certs(config)
    if ca_certs is not None:
        client_kwargs["ca_certs"] = ca_certs
    if config.connect_timeout is not None:
        client_kwargs["connect_timeout"] = config.connect_timeout
    if config.send_receive_timeout is not None:
        client_kwargs["send_receive_timeout"] = config.send_receive_timeout
    if config.settings is not None:
        client_kwargs["settings"] = config.settings
    if config.client_name is not None:
        client_kwargs["client_name"] = config.client_name
    return NativeClickHouseClient(Client(**client_kwargs))
