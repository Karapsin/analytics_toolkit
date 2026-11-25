"""Freeze the JSON documents accepted by Greenplum's existing insert transport."""

from __future__ import annotations

import json
from decimal import Decimal
from functools import lru_cache
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlglot import Dialect, exp
from sqlglot.errors import ParseError, TokenError

from analytics_toolkit.sql.backends import native_scalars
from analytics_toolkit.sql.backends.native_scalars import ExactArray

from .source_arrays import map_array_values

JsonDocument = native_scalars.JsonDocument

if TYPE_CHECKING:
    from collections.abc import Mapping
    from typing import List

    import pandas as pd

    from analytics_toolkit.sql.backends.value_profiles import ColumnProfile

    _JsonArrayBase = List[Any]
else:
    _JsonArrayBase = list

_MAX_INTEGRAL_DIGITS = 131072
_MAX_FRACTIONAL_DIGITS = 16383
_MAX_ARRAY_DIMENSIONS = 6


def prepare_dataframe_payload(
    self: Any, df: pd.DataFrame, schema: Mapping[str, str] | None
) -> pd.DataFrame | None:
    """Own JSON documents and exact temporal components before mutation/replay."""
    del self
    from .network_values import (  # noqa: PLC0415 -- own the optional driver's known wrapper.
        is_network_type,
        normalize_authoritative_network,
    )
    from .temporal_values import (  # noqa: PLC0415 -- runtime dataframe codec.
        freeze_clock_interval_value,
        is_gp_clock_interval_type,
        temporal_family,
    )
    from .type_identity import canonical_builtin_type  # noqa: PLC0415 -- native identity.

    def is_temporal(native: str) -> bool:
        native = canonical_builtin_type(native) or native
        while native.rstrip().endswith("[]"):
            native = native.rstrip()[:-2]
        return is_gp_clock_interval_type(native)

    if schema is None:
        inferred = {}
        for index, name in enumerate(df.columns):
            families = {
                temporal_family(_logical_value(value))
                for value in df.iloc[:, index]
                if not _is_null(_logical_value(value))
            }
            if families in ({"time"}, {"interval"}):
                inferred[str(name)] = "TIME" if families == {"time"} else "INTERVAL"
        schema = inferred

    payload_columns = [
        (index, name, schema[str(name)])
        for index, name in enumerate(df.columns)
        if str(name) in schema
        and (
            is_json_type(schema[str(name)])
            or is_temporal(schema[str(name)])
            or is_network_type(schema[str(name)])
        )
    ]
    if not payload_columns:
        return None
    import pandas as pd  # noqa: PLC0415 -- dataframe codec boundary.

    prepared = df.copy(deep=False)
    for index, name, native in payload_columns:
        # Replacing the column leaves the caller's buffers and other dtypes intact.
        prepared[name] = pd.Series(
            [
                convert_json_value(value, native)
                if is_json_type(native)
                else normalize_authoritative_network(value, native)
                if is_network_type(native)
                else _freeze_temporal_payload(value, native, freeze_clock_interval_value)
                for value in df.iloc[:, index]
            ],
            index=df.index,
            dtype=object,
        )
    return prepared


def _freeze_temporal_payload(value: Any, native: str, freeze: Any) -> Any:
    from .temporal_assignment import (  # noqa: PLC0415 -- value-aware native parameter policy.
        temporal_assignment_value,
        temporal_text_value_required,
    )

    if temporal_text_value_required(value, native):
        return temporal_assignment_value(value, native)
    return freeze(value, native)


class JsonArray(_JsonArrayBase):
    """Owned native SQL-array topology, independent of catalog-erased rank."""

    __slots__ = ()


def _logical_value(value: Any) -> Any:
    from analytics_toolkit.sql.core.exact_values import (  # noqa: PLC0415 -- registry cycle.
        unwrap_enum,
    )

    return unwrap_enum(value)


def _is_null(value: Any) -> bool:
    from analytics_toolkit.sql.core.exact_values import is_null  # noqa: PLC0415 -- registry cycle.

    return is_null(value)


def _json_name(native: str) -> str:
    if native.lower() in {"json", "jsonb"}:
        return native.lower()
    try:
        parsed = Dialect.get_or_raise("postgres").parse_into(exp.Table, native)
    except (ParseError, TokenError):
        return ""
    if len(parsed) != 1 or not isinstance(parsed[0], exp.Table):
        return ""
    table = parsed[0]
    if any(value is not None for key, value in table.args.items() if key not in {"this", "db"}):
        return ""
    parts = [table.args["db"], table.this] if table.args.get("db") is not None else [table.this]
    if not all(isinstance(part, exp.Identifier) for part in parts):
        return ""
    names = [part.name if part.args.get("quoted") else part.name.lower() for part in parts]
    if names[:-1] == ["pg_catalog"]:
        names = names[1:]
    return names[0] if len(names) == 1 and names[0] in {"json", "jsonb"} else ""


@lru_cache(maxsize=1024)
def _json_type(native_type: str) -> tuple[str, int]:
    native = native_type.strip()
    rank = 0
    while native.endswith("[]"):
        native = native[:-2].rstrip()
        rank += 1
    name = _json_name(native)
    return (name, rank) if name else ("", 0)


def is_json_type(native_type: str) -> bool:
    return bool(_json_type(native_type)[0])


def _reject_constant(_constant: str) -> None:
    message = "JSON requires finite numeric tokens."
    raise ValueError(message)


def _validate_jsonb(value: Any) -> None:
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, str):
            if "\x00" in item:
                message = "JSONB cannot contain a Unicode zero."
                raise ValueError(message)
            item.encode("utf-8")
        elif isinstance(item, Decimal):
            _sign, digits, exponent = item.as_tuple()
            integral = max(len(digits) + int(exponent), 0) if item else 0
            fractional = max(-int(exponent), 0)
            if integral > _MAX_INTEGRAL_DIGITS or fractional > _MAX_FRACTIONAL_DIGITS:
                message = "JSONB numeric token exceeds the Greenplum numeric domain."
                raise ValueError(message)
        elif isinstance(item, (list, tuple)):
            pending.extend(item)


def _validate_document(document: str, native: str) -> JsonDocument:
    # Keep the original text, including whitespace, number spelling and duplicate
    # keys. Pairs retain earlier duplicate values for JSONB constraint checks.
    document.encode("utf-8")
    number = Decimal if native == "jsonb" else str
    decoded = json.loads(
        document,
        parse_int=number,
        parse_float=number,
        parse_constant=_reject_constant,
        object_pairs_hook=tuple,
    )
    if native == "jsonb":
        _validate_jsonb(decoded)
    return JsonDocument(document)


def _is_json_adapter(value: Any) -> bool:
    try:
        from psycopg2.extras import Json  # noqa: PLC0415 -- optional backend transport.
    except ImportError:
        return False
    return isinstance(value, Json)


def _normalize_document_value(value: Any) -> Any:
    value = _logical_value(value)
    if isinstance(value, dict):
        return {key: _normalize_document_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize_document_value(item) for item in value]
    if _is_null(value):
        return None
    return str(value) if isinstance(value, UUID) else value


def _document(value: Any, native: str, *, logical_leaf: bool) -> JsonDocument:
    value = _logical_value(value)
    if isinstance(value, JsonDocument):
        document = str(value)
    elif _is_json_adapter(value):
        document = value.dumps(value.adapted)
    elif isinstance(value, str) and not logical_leaf:
        document = value
    elif logical_leaf or isinstance(value, (dict, list)):
        document = json.dumps(_normalize_document_value(value))
    else:
        message = "Scalar JSON inputs require a dictionary, list, encoded document or Json adapter."
        raise TypeError(message)
    if not isinstance(document, str):
        message = "JSON serialization must produce text."
        raise TypeError(message)
    return _validate_document(document, native)


def _array_result(
    children: list[tuple[Any, tuple[int, ...] | None]],
) -> tuple[JsonArray, tuple[int, ...]]:
    shapes = [shape for _item, shape in children if shape is not None]
    if shapes and (
        any(shape != shapes[0] for shape in shapes) or (shapes[0] and len(shapes) != len(children))
    ):
        message = "JSON arrays must be rectangular."
        raise ValueError(message)
    shape = (len(children), *(shapes[0] if shapes else ()))
    if len(shape) > _MAX_ARRAY_DIMENSIONS:
        message = "JSON arrays exceed Greenplum's maximum dimensions."
        raise ValueError(message)
    return JsonArray(item for item, _shape in children), shape


def _frozen_array_documents(value: JsonArray, native: str) -> tuple[JsonArray, tuple[int, ...]]:
    children: list[tuple[Any, tuple[int, ...] | None]] = []
    for item in value:
        if isinstance(item, JsonArray):
            children.append(_frozen_array_documents(item, native))
        else:
            children.append(
                (None, None)
                if _is_null(item)
                else (_document(item, native, logical_leaf=False), ())
            )
    return _array_result(children)


def _array_documents(value: Any, native: str, rank: int) -> tuple[Any, tuple[int, ...] | None]:
    value = _logical_value(value)
    if _is_null(value):
        return None, None
    if isinstance(value, JsonArray):
        return _frozen_array_documents(value, native)
    if not isinstance(value, (list, tuple)):
        message = "JSON arrays must match the destination rank."
        raise TypeError(message)
    if rank == 1:
        return _array_result(
            [
                (None, None) if _is_null(item) else (_document(item, native, logical_leaf=True), ())
                for item in map(_logical_value, value)
            ]
        )
    return _array_result([_array_documents(item, native, rank - 1) for item in value])


def convert_json_value(value: Any, native_type: str, *, decoded: bool = False) -> Any:
    """Certify and freeze actual documents without re-running a custom serializer."""
    native, rank = _json_type(native_type)
    if not native:
        message = "A Greenplum JSON or JSONB destination type is required."
        raise ValueError(message)
    value = _logical_value(value)
    if _is_null(value):
        return None
    try:
        if rank:
            if isinstance(value, ExactArray):
                return map_array_values(
                    value,
                    lambda item: (
                        None if _is_null(item) else _document(item, native, logical_leaf=True)
                    ),
                )
            return _array_documents(value, native, rank)[0]
        return _document(value, native, logical_leaf=decoded)
    except Exception:  # noqa: BLE001 -- custom serializers may raise payload-bearing errors.
        # Serializer/parser messages may contain private values. Keep validation
        # failures deterministic and omit their payload-bearing exception chain.
        message = (
            "Selected input is incompatible with authoritative Greenplum JSON serialization "
            f"({native.upper()})."
        )
        raise ValueError(message) from None


def validate_json_profile(profile: ColumnProfile, native_type: str) -> bool:
    """Register only supported JSON families; per-value conversion certifies syntax."""
    native, rank = _json_type(native_type)
    if not native:
        return False
    families = (
        {"array", "tuple"} if rank else {"map", "array", "string", "integer", "float", "boolean"}
    )
    if not profile.families <= families:
        message = "Selected input is incompatible with authoritative Greenplum JSON values."
        raise ValueError(message)
    return True
