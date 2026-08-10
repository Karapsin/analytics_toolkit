from __future__ import annotations

from typing import Any, Literal, cast

ChDdlWaitPolicy = Literal["wait_all", "wait_shard", "wait_distr", "wait_none"]

DEFAULT_CH_DDL_WAIT_POLICY: ChDdlWaitPolicy = "wait_all"
CH_DDL_WAIT_POLICIES = frozenset({"wait_all", "wait_shard", "wait_distr", "wait_none"})


def parse_ch_ddl_wait_policy(
    value: Any,
    *,
    option_name: str = "ch_ddl_wait_policy",
    error_type: type[Exception] = ValueError,
) -> ChDdlWaitPolicy:
    if isinstance(value, str) and value in CH_DDL_WAIT_POLICIES:
        return cast("ChDdlWaitPolicy", value)
    accepted = ", ".join(sorted(CH_DDL_WAIT_POLICIES))
    message = f"{option_name} must be one of: {accepted}."
    raise error_type(message)


def resolve_ch_ddl_wait_policy(
    explicit: str | None,
    configured: str | None,
) -> ChDdlWaitPolicy:
    return parse_ch_ddl_wait_policy(
        explicit if explicit is not None else configured or DEFAULT_CH_DDL_WAIT_POLICY,
    )


def parse_connection_ch_ddl_wait_policy(value: Any) -> ChDdlWaitPolicy:
    # Import lazily because connection.config imports the backend registry.
    from analytics_toolkit.sql.connection.errors import SqlConfigError  # noqa: PLC0415

    return parse_ch_ddl_wait_policy(
        value,
        option_name="ClickHouse connection field 'ch_ddl_wait_policy'",
        error_type=SqlConfigError,
    )


def waits_for_shard(policy: str) -> bool:
    return policy in {"wait_all", "wait_shard"}


def waits_for_distributed(policy: str) -> bool:
    return policy in {"wait_all", "wait_distr"}


__all__ = [
    "CH_DDL_WAIT_POLICIES",
    "DEFAULT_CH_DDL_WAIT_POLICY",
    "ChDdlWaitPolicy",
    "parse_ch_ddl_wait_policy",
    "parse_connection_ch_ddl_wait_policy",
    "resolve_ch_ddl_wait_policy",
    "waits_for_distributed",
    "waits_for_shard",
]
