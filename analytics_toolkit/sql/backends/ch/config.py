from __future__ import annotations

from typing import Any, Callable, Literal

from analytics_toolkit.sql.connection.errors import SqlConfigError
from analytics_toolkit.sql.connection.validation import validating, validation_timeout
from analytics_toolkit.sql.execution.backend_options import validate_ch_unavailable_shards
from analytics_toolkit.sql.execution.observation import observed_connection

from .outage_client import OutageClient
from .outage_context import INTERNAL_IO, internal_io
from .routing import parse_cluster_routing
from .wait_policy import parse_connection_ch_ddl_wait_policy

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
    "ddl_ready_timeout_seconds",
    "ddl_ready_timeout_extension_cnt",
    "ch_ddl_wait_policy",
    "client_name",
    "compression",
    "ddl_defaults",
    "cluster_routing",
    "ch_unavailable_shards",
)


def build_config(connection_key: str, raw_config: dict[str, Any]) -> Any:
    from analytics_toolkit.sql.connection.config import (
        ChConfig,
        _optional_bool,
        _optional_int,
        _optional_mapping,
        _optional_non_negative_int,
        _optional_positive_int,
        _optional_string,
        _optional_string_or_string_list,
        _reject_removed_fields,
        _require_string,
    )
    from analytics_toolkit.sql.connection.ddl_defaults import (  # noqa: PLC0415
        parse_ddl_defaults,
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
        verify_value=_parse_ch_verify(raw_config, connection_key),
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
        ddl_ready_timeout_seconds=_optional_positive_int(
            raw_config,
            connection_key,
            "ddl_ready_timeout_seconds",
        ),
        ddl_ready_timeout_extension_cnt=_optional_non_negative_int(
            raw_config,
            connection_key,
            "ddl_ready_timeout_extension_cnt",
        ),
        ch_ddl_wait_policy=(
            parse_connection_ch_ddl_wait_policy(raw_config["ch_ddl_wait_policy"])
            if "ch_ddl_wait_policy" in raw_config
            else None
        ),
        client_name=_optional_string(raw_config, connection_key, "client_name"),
        compression=_parse_compression(raw_config, connection_key, SqlConfigError),
        transfer_staging_schema=_optional_string(
            raw_config,
            connection_key,
            "transfer_staging_schema",
        ),
        ddl_defaults=parse_ddl_defaults(
            raw_config.get("ddl_defaults"),
            connection_key,
            "ch",
        ),
        cluster_routing=parse_cluster_routing(
            raw_config.get("cluster_routing"),
            connection_key,
        ),
        ch_unavailable_shards=validate_ch_unavailable_shards(
            raw_config.get("ch_unavailable_shards", "stop"), error_type=SqlConfigError
        ),
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
        client = _open_native_connection(
            config,
            parse_verify_value=parse_verify_value,
            resolve_ch_ca_certs=resolve_ch_ca_certs,
        )
    else:
        client = _open_http_connection(
            config,
            parse_verify_value=parse_verify_value,
            resolve_ch_ca_certs=resolve_ch_ca_certs,
        )

    client = observed_connection(client)
    return client if INTERNAL_IO.get() else OutageClient(client, config)


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
    connect_timeout = validation_timeout(config.connect_timeout)
    read_timeout = validation_timeout(config.send_receive_timeout)
    if connect_timeout is not None:
        client_kwargs["connect_timeout"] = connect_timeout
    if read_timeout is not None:
        client_kwargs["send_receive_timeout"] = read_timeout
    if config.settings is not None:
        client_kwargs["settings"] = config.settings
    if validating():
        client_kwargs["settings"] = {
            **(config.settings or {}),
            "max_execution_time": min(
                float((config.settings or {}).get("max_execution_time", 5) or 5), 5
            ),
        }
    if config.interface is not None:
        client_kwargs["interface"] = config.interface
    if config.query_limit is not None:
        client_kwargs["query_limit"] = config.query_limit
    if validating() or config.query_retries is not None:
        client_kwargs["query_retries"] = 0 if validating() else config.query_retries
    if config.client_name is not None:
        client_kwargs["client_name"] = config.client_name

    from .http_codec import ExactHTTPClient  # noqa: PLC0415

    return ExactHTTPClient(clickhouse_connect.get_client(**client_kwargs))


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
    connect_timeout = validation_timeout(config.connect_timeout)
    read_timeout = validation_timeout(config.send_receive_timeout)
    if connect_timeout is not None:
        client_kwargs["connect_timeout"] = connect_timeout
    if read_timeout is not None:
        client_kwargs["send_receive_timeout"] = read_timeout
    if config.settings is not None:
        client_kwargs["settings"] = config.settings
    if validating():
        client_kwargs["settings"] = {
            **(config.settings or {}),
            "max_execution_time": min(
                float((config.settings or {}).get("max_execution_time", 5) or 5), 5
            ),
        }
    if config.client_name is not None:
        client_kwargs["client_name"] = config.client_name
    if validating():
        client_kwargs["disable_reconnect"] = True
    return NativeClickHouseClient(Client(**client_kwargs))


def _parse_ch_verify(raw_config: dict[str, Any], connection_key: str) -> str | None:
    value = raw_config.get("verify")
    if value is None:
        return None
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
        return value.strip().lower()
    message = (
        f"SQL connection '{connection_key}' field 'verify' must be true or false; "
        "use ca_certs for certificate paths."
    )
    raise SqlConfigError(message)


def validate_connection(adapter: Any, connection: Any) -> None:
    del adapter
    with internal_io():
        result = connection.query("SELECT 1")
        rows = result.result_rows
        if not rows or rows[0][0] != 1:
            message = "Connectivity probe returned an unexpected result."
            raise RuntimeError(message)
