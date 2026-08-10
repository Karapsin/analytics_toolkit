from __future__ import annotations

from typing import Any


def parquet_storage_options(config: Any) -> dict[str, Any] | None:
    access_key_id = getattr(config, "access_key_id", None)
    secret_access_key = getattr(config, "secret_access_key", None)
    endpoint_url = getattr(config, "endpoint_url", None)
    options: dict[str, Any] = {}
    if access_key_id is not None and secret_access_key is not None:
        options.update(key=str(access_key_id), secret=str(secret_access_key))
    if endpoint_url is not None:
        options["client_kwargs"] = {"endpoint_url": str(endpoint_url)}
    return options or None


__all__ = ["parquet_storage_options"]
