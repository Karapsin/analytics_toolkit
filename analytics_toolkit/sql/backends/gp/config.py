from __future__ import annotations

from typing import Any, Callable


def build_config(connection_key: str, raw_config: dict[str, Any]) -> Any:
    from analytics_toolkit.sql.connection.ddl_defaults import (  # noqa: PLC0415
        parse_ddl_defaults,
    )

    from ...connection.config import (
        DEFAULT_GP_CONNECT_TIMEOUT_SECONDS,
        DEFAULT_GP_KEEPALIVES,
        DEFAULT_GP_KEEPALIVES_COUNT,
        DEFAULT_GP_KEEPALIVES_IDLE_SECONDS,
        DEFAULT_GP_KEEPALIVES_INTERVAL_SECONDS,
        GpConfig,
        _optional_bool,
        _optional_int,
        _optional_string,
        _optional_string_or_string_list,
        _reject_removed_fields,
        _require_string,
    )

    _reject_removed_fields(raw_config, connection_key, ["ca_cert"])
    ca_certs = _optional_string_or_string_list(
        raw_config,
        connection_key,
        "ca_certs",
    )
    sslmode = _optional_string(raw_config, connection_key, "sslmode")
    if ca_certs and sslmode is None:
        sslmode = "verify-full"
    return GpConfig(
        connection_key=connection_key,
        backend="gp",
        host=_require_string(raw_config, connection_key, "host"),
        port=_optional_int(raw_config, connection_key, "port", 5432),
        user=_require_string(raw_config, connection_key, "user"),
        password=_require_string(raw_config, connection_key, "password"),
        database=_require_string(raw_config, connection_key, "database"),
        connect_timeout=_optional_int(
            raw_config,
            connection_key,
            "connect_timeout",
            DEFAULT_GP_CONNECT_TIMEOUT_SECONDS,
        ),
        keepalives=_optional_bool(
            raw_config,
            connection_key,
            "keepalives",
            DEFAULT_GP_KEEPALIVES,
        ),
        keepalives_idle=_optional_int(
            raw_config,
            connection_key,
            "keepalives_idle",
            DEFAULT_GP_KEEPALIVES_IDLE_SECONDS,
        ),
        keepalives_interval=_optional_int(
            raw_config,
            connection_key,
            "keepalives_interval",
            DEFAULT_GP_KEEPALIVES_INTERVAL_SECONDS,
        ),
        keepalives_count=_optional_int(
            raw_config,
            connection_key,
            "keepalives_count",
            DEFAULT_GP_KEEPALIVES_COUNT,
        ),
        sslmode=sslmode,
        ca_certs=ca_certs,
        ssl_cert=_optional_string(raw_config, connection_key, "ssl_cert"),
        ssl_key=_optional_string(raw_config, connection_key, "ssl_key"),
        transfer_staging_schema=_optional_string(
            raw_config,
            connection_key,
            "transfer_staging_schema",
        ),
        ddl_defaults=parse_ddl_defaults(raw_config.get("ddl_defaults"), connection_key, "gp"),
    )


def open_connection(
    config: Any,
    *,
    resolve_ca_certs: Callable[[str, list[str]], str | None],
    resolve_single_cert_path: Callable[[str, str, str], Any],
) -> Any:
    try:
        import psycopg2
    except ImportError as exc:
        raise ImportError("The 'psycopg2' package is required for Greenplum connections.") from exc

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
    ca_certs = resolve_ca_certs(config.connection_key, config.ca_certs)
    if ca_certs is not None:
        connect_kwargs["sslrootcert"] = ca_certs
    if config.ssl_cert is not None:
        ssl_cert = resolve_single_cert_path(
            config.connection_key,
            config.ssl_cert,
            "ssl_cert",
        )
        connect_kwargs["sslcert"] = str(ssl_cert)
    if config.ssl_key is not None:
        ssl_key = resolve_single_cert_path(
            config.connection_key,
            config.ssl_key,
            "ssl_key",
        )
        connect_kwargs["sslkey"] = str(ssl_key)

    return psycopg2.connect(**connect_kwargs)
