from __future__ import annotations

from typing import Any, Callable


def build_config(connection_key: str, raw_config: dict[str, Any]) -> Any:
    from ...connection.config import (
        TrinoConfig,
        _optional_bool_or_string_as_string,
        _optional_positive_int,
        _optional_string,
        _optional_string_or_string_list,
        _reject_removed_fields,
        _require_string,
        _optional_int,
    )

    _reject_removed_fields(
        raw_config,
        connection_key,
        ["use_keychain_certs", "keychain_cert_names", "ca_cert"],
    )
    return TrinoConfig(
        connection_key=connection_key,
        backend="trino",
        host=_require_string(raw_config, connection_key, "host"),
        port=_optional_int(raw_config, connection_key, "port", 8080),
        user=_require_string(raw_config, connection_key, "user"),
        password=_optional_string(raw_config, connection_key, "password"),
        catalog=_optional_string(raw_config, connection_key, "catalog"),
        schema=_optional_string(raw_config, connection_key, "schema"),
        auth_mode=_optional_string(
            raw_config,
            connection_key,
            "auth_mode",
            "basic",
        ).lower(),
        http_scheme=_optional_string(
            raw_config,
            connection_key,
            "http_scheme",
            "http",
        ),
        verify_value=(
            _optional_bool_or_string_as_string(
                raw_config,
                connection_key,
                "verify",
            )
            or "true"
        ),
        transfer_staging_schema=_optional_string(
            raw_config,
            connection_key,
            "transfer_staging_schema",
        ),
        transfer_staging_location=_optional_string(
            raw_config,
            connection_key,
            "transfer_staging_location",
        ),
        ca_certs=_optional_string_or_string_list(
            raw_config,
            connection_key,
            "ca_certs",
        ),
        insert_chunk_size=_optional_positive_int(
            raw_config,
            connection_key,
            "insert_chunk_size",
        ),
        request_timeout=_optional_positive_int(
            raw_config,
            connection_key,
            "request_timeout",
        ),
        source=_optional_string(raw_config, connection_key, "source"),
    )


def open_connection(
    config: Any,
    *,
    parse_verify_value: Callable[[str], bool | str],
    resolve_ca_certs: Callable[[str, list[str]], str | None],
) -> Any:
    try:
        import trino
        from trino.auth import BasicAuthentication
    except ImportError as exc:
        raise ImportError(
            "The 'trino' package is required for Trino connections."
        ) from exc

    ca_certs = resolve_ca_certs(config.connection_key, config.ca_certs)
    verify_value = ca_certs or config.verify_value

    if config.auth_mode == "oauth2":
        auth = trino.auth.OAuth2Authentication()
    elif config.auth_mode == "basic":
        auth = BasicAuthentication(config.user, config.password) if config.password else None
    else:
        from ...connection.errors import SqlConfigError

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
        "verify": parse_verify_value(verify_value),
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
