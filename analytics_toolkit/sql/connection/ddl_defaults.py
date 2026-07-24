from __future__ import annotations

# ruff: noqa: E501, EM102, FBT003, FURB171, I001, PLR0913, SIM101, SIM102, TRY003

import math
import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

from .errors import SqlConfigError

_PROPERTY_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MISSING = object()


@dataclass(frozen=True)
class ClickHouseObjectDefaults:
    engine: str | None | object = _MISSING
    on_cluster: str | None | object = _MISSING


@dataclass(frozen=True)
class ClickHouseDistributedDefaults:
    engine_template: str | None | object = _MISSING
    cluster: str | None | object = _MISSING
    on_cluster: str | None | object = _MISSING
    sharding_key: str | None | object = _MISSING


@dataclass(frozen=True)
class ClickHouseScopeDefaults:
    create_distributed_pair: bool | object = _MISSING
    shard: ClickHouseObjectDefaults = ClickHouseObjectDefaults()
    distributed: ClickHouseDistributedDefaults = ClickHouseDistributedDefaults()


@dataclass(frozen=True)
class DdlDefaults:
    regular: Mapping[str, Any] | ClickHouseScopeDefaults
    staging: Mapping[str, Any] | ClickHouseScopeDefaults
    parquet_staging: Mapping[str, Any] | None = None


def empty_property_defaults(*, trino: bool = False) -> DdlDefaults:
    empty: Mapping[str, Any] = MappingProxyType({})
    return DdlDefaults(empty, empty, empty if trino else None)


def parse_ddl_defaults(raw: Any, connection_key: str, backend: str) -> DdlDefaults:
    if raw is None:
        if backend in ("ch",):
            return DdlDefaults(ClickHouseScopeDefaults(), ClickHouseScopeDefaults())
        return empty_property_defaults(trino=backend in ("trino",))
    if not isinstance(raw, dict):
        raise SqlConfigError(
            f"SQL connection '{connection_key}' field 'ddl_defaults' must be a JSON object."
        )
    allowed = (
        {"regular", "staging", "parquet_staging"}
        if backend in ("trino",)
        else {"regular", "staging"}
    )
    unexpected = set(raw) - allowed
    if unexpected:
        names = ", ".join(sorted(unexpected))
        raise SqlConfigError(
            f"SQL connection '{connection_key}' ddl_defaults has unsupported scope(s) for {backend}: {names}."
        )
    if backend in ("ch",):
        return DdlDefaults(
            _parse_ch_scope(raw.get("regular", {}), connection_key, "regular"),
            _parse_ch_scope(raw.get("staging", {}), connection_key, "staging"),
        )
    regular = _parse_property_scope(raw.get("regular", {}), connection_key, "regular")
    staging = _parse_property_scope(raw.get("staging", {}), connection_key, "staging")
    parquet = (
        _parse_property_scope(raw.get("parquet_staging", {}), connection_key, "parquet_staging")
        if backend in ("trino",)
        else None
    )
    return DdlDefaults(regular, staging, parquet)


def _parse_property_scope(raw: Any, key: str, scope: str) -> Mapping[str, Any]:
    if not isinstance(raw, dict):
        raise SqlConfigError(f"SQL connection '{key}' ddl_defaults.{scope} must be a JSON object.")
    normalized: dict[str, Any] = {}
    for property_key, value in raw.items():
        if not isinstance(property_key, str) or not _PROPERTY_KEY.fullmatch(property_key):
            raise SqlConfigError(
                f"SQL connection '{key}' ddl_defaults.{scope} has invalid SQL property key {property_key!r}."
            )
        normalized_key = property_key.lower()
        if normalized_key in normalized:
            raise SqlConfigError(
                f"SQL connection '{key}' ddl_defaults.{scope} has duplicate property '{normalized_key}' after normalization."
            )
        _validate_property_value(value, key, scope, normalized_key)
        normalized[normalized_key] = value
    return MappingProxyType(normalized)


def _validate_property_value(value: Any, key: str, scope: str, name: str) -> None:
    if value is None or isinstance(value, bool) or isinstance(value, int):
        return
    if isinstance(value, float):
        if math.isfinite(value):
            return
    elif isinstance(value, str):
        if value.strip():
            return
    elif isinstance(value, list):
        if all(
            item is None
            or isinstance(item, (bool, int, str))
            or (isinstance(item, float) and math.isfinite(item))
            for item in value
        ):
            return
    raise SqlConfigError(
        f"SQL connection '{key}' ddl_defaults.{scope}.{name} has an unsupported property value."
    )


def _parse_ch_scope(raw: Any, key: str, scope: str) -> ClickHouseScopeDefaults:
    if not isinstance(raw, dict):
        raise SqlConfigError(f"SQL connection '{key}' ddl_defaults.{scope} must be a JSON object.")
    unexpected = set(raw) - {"create_distributed_pair", "shard", "distributed"}
    if unexpected:
        raise SqlConfigError(
            f"SQL connection '{key}' ddl_defaults.{scope} has unsupported ClickHouse field(s): {', '.join(sorted(unexpected))}."
        )
    pair: bool | object = _MISSING
    if "create_distributed_pair" in raw:
        pair = raw["create_distributed_pair"]
        if not isinstance(pair, bool):
            raise SqlConfigError(
                f"SQL connection '{key}' ddl_defaults.{scope}.create_distributed_pair must be a boolean."
            )
    shard = _parse_ch_object(raw.get("shard", {}), key, scope)
    distributed = _parse_ch_distributed(raw.get("distributed", {}), key, scope)
    return ClickHouseScopeDefaults(pair, shard, distributed)


def _parse_ch_object(raw: Any, key: str, scope: str) -> ClickHouseObjectDefaults:
    _require_mapping(raw, key, scope, "shard")
    _reject_keys(raw, {"engine", "on_cluster"}, key, scope, "shard")
    return ClickHouseObjectDefaults(
        _optional_non_empty(raw, "engine", key, scope, "shard", allow_null=False),
        _optional_non_empty(raw, "on_cluster", key, scope, "shard", allow_null=True),
    )


def _parse_ch_distributed(raw: Any, key: str, scope: str) -> ClickHouseDistributedDefaults:
    _require_mapping(raw, key, scope, "distributed")
    fields = {"engine_template", "cluster", "on_cluster", "sharding_key"}
    _reject_keys(raw, fields, key, scope, "distributed")
    return ClickHouseDistributedDefaults(
        *(
            _optional_non_empty(
                raw, name, key, scope, "distributed", allow_null=name == "on_cluster"
            )
            for name in ("engine_template", "cluster", "on_cluster", "sharding_key")
        )
    )


def _require_mapping(raw: Any, key: str, scope: str, name: str) -> None:
    if not isinstance(raw, dict):
        raise SqlConfigError(
            f"SQL connection '{key}' ddl_defaults.{scope}.{name} must be a JSON object."
        )


def _reject_keys(
    raw: Mapping[str, Any], allowed: set[str], key: str, scope: str, name: str
) -> None:
    unexpected = set(raw) - allowed
    if unexpected:
        raise SqlConfigError(
            f"SQL connection '{key}' ddl_defaults.{scope}.{name} has unsupported field(s): {', '.join(sorted(unexpected))}."
        )


def _optional_non_empty(
    raw: Mapping[str, Any], name: str, key: str, scope: str, parent: str, *, allow_null: bool
) -> str | None | object:
    if name not in raw:
        return _MISSING
    value = raw[name]
    if value is None and allow_null:
        return None
    if not isinstance(value, str) or not value.strip():
        suffix = " or null" if allow_null else ""
        raise SqlConfigError(
            f"SQL connection '{key}' ddl_defaults.{scope}.{parent}.{name} must be a non-empty string{suffix}."
        )
    return value.strip()


MISSING_DDL_VALUE = _MISSING


def legacy_clickhouse_scope(*, staging: bool = False) -> ClickHouseScopeDefaults:
    if staging:
        return ClickHouseScopeDefaults(False, ClickHouseObjectDefaults("MergeTree", None))
    return ClickHouseScopeDefaults(
        True,
        ClickHouseObjectDefaults("ReplicatedMergeTree", "{cluster}"),
        ClickHouseDistributedDefaults(
            "Distributed({cluster}, {database}, {shard_table}, {sharding_key})",
            "{cluster}",
            "{cluster}",
            "rand()",
        ),
    )
