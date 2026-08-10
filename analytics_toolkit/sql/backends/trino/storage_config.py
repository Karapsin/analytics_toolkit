from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, NoReturn

if TYPE_CHECKING:
    from collections.abc import Callable


@dataclass(frozen=True)
class TrinoStorageConfig:
    access_key_id: str | None
    secret_access_key: str | None
    endpoint_url: str | None
    staging_schema: str | None
    staging_location: str | None


def resolve_storage_config(
    raw_config: dict[str, Any],
    connection_key: str,
    optional_string: Callable[[dict[str, Any], str, str], str | None],
) -> TrinoStorageConfig:
    _reject_removed_and_session_fields(raw_config, connection_key)
    access_key_id, secret_access_key = _resolve_credentials(
        raw_config,
        connection_key,
        optional_string,
    )
    endpoint_url = _resolve_endpoint(
        raw_config,
        connection_key,
        optional_string,
    )
    staging_schema = optional_string(
        raw_config,
        connection_key,
        "s3_transfer_staging_schema",
    )
    staging_location = optional_string(
        raw_config,
        connection_key,
        "s3_transfer_staging_location",
    )
    if (staging_schema is None) != (staging_location is None):
        message = (
            f"SQL connection '{connection_key}' fields 's3_transfer_staging_schema' "
            "and 's3_transfer_staging_location' must be supplied together."
        )
        _raise_config_error(message)
    return TrinoStorageConfig(
        access_key_id,
        secret_access_key,
        endpoint_url,
        staging_schema,
        staging_location,
    )


def _reject_removed_and_session_fields(raw_config: dict[str, Any], connection_key: str) -> None:
    removed = [
        field
        for field in ("transfer_staging_location", "transfer_parquet_staging_schema")
        if field in raw_config
    ]
    if removed:
        fields = ", ".join(repr(field) for field in removed)
        message = (
            f"SQL connection '{connection_key}' uses removed Trino staging field(s): "
            f"{fields}. Use 's3_transfer_staging_schema' together with "
            "'s3_transfer_staging_location'."
        )
        _raise_config_error(message)
    unsupported = [field for field in ("session_token", "aws_session_token") if field in raw_config]
    if unsupported:
        fields = ", ".join(repr(field) for field in unsupported)
        message = (
            f"SQL connection '{connection_key}' has unsupported Trino Parquet "
            f"credential field(s): {fields}. Session tokens are not supported."
        )
        _raise_config_error(message)


def _resolve_credentials(
    raw_config: dict[str, Any],
    connection_key: str,
    optional_string: Callable[[dict[str, Any], str, str], str | None],
) -> tuple[str | None, str | None]:
    families = (
        ("aws_access_key_id", "aws_secret_access_key"),
        ("access_key_id", "secret_access_key"),
    )
    supplied = [family for family in families if any(field in raw_config for field in family)]
    if len(supplied) > 1:
        message = (
            f"SQL connection '{connection_key}' must use exactly one Trino Parquet "
            "credential family; AWS-prefixed and unprefixed fields cannot be mixed."
        )
        _raise_config_error(message)
    if not supplied:
        return None, None
    access_field, secret_field = supplied[0]
    access_key_id = optional_string(raw_config, connection_key, access_field)
    secret_access_key = optional_string(raw_config, connection_key, secret_field)
    if access_key_id is None or secret_access_key is None:
        message = (
            f"SQL connection '{connection_key}' fields '{access_field}' and "
            f"'{secret_field}' must be supplied together."
        )
        _raise_config_error(message)
    return access_key_id, secret_access_key


def _resolve_endpoint(
    raw_config: dict[str, Any],
    connection_key: str,
    optional_string: Callable[[dict[str, Any], str, str], str | None],
) -> str | None:
    supplied = [field for field in ("aws_endpoint_url", "endpoint_url") if field in raw_config]
    if len(supplied) > 1:
        message = (
            f"SQL connection '{connection_key}' must specify only one of "
            "'aws_endpoint_url' and 'endpoint_url'."
        )
        _raise_config_error(message)
    return optional_string(raw_config, connection_key, supplied[0]) if supplied else None


def _raise_config_error(message: str) -> NoReturn:
    # Import lazily because connection.config imports the backend registry.
    from analytics_toolkit.sql.connection.errors import SqlConfigError  # noqa: PLC0415

    raise SqlConfigError(message)


__all__ = ["TrinoStorageConfig", "resolve_storage_config"]
