from __future__ import annotations

from typing import Any, Callable


def build_config(connection_key: str, raw_config: dict[str, Any]) -> Any:
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
        host=_require_string(raw_config, connection_key, "host"),
        port=_optional_int(raw_config, connection_key, "port", 8123),
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
        transfer_staging_schema=_optional_string(
            raw_config,
            connection_key,
            "transfer_staging_schema",
        ),
    )


def open_connection(
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
