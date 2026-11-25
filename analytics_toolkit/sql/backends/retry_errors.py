"""Classify backend driver failures without importing optional SDKs eagerly."""

from __future__ import annotations


def is_non_retryable_driver_error(error: Exception) -> bool:
    modules = tuple(cls.__module__ for cls in type(error).__mro__)
    if any(module.startswith(("clickhouse_driver.", "clickhouse_connect.")) for module in modules):
        from .ch import retry_errors  # noqa: PLC0415 -- optional backend dispatch.

        return retry_errors.is_permission_error(error)
    return False
