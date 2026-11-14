from __future__ import annotations

import re
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from uuid import UUID

import pandas as pd

if TYPE_CHECKING:
    from collections.abc import Sequence

    from analytics_toolkit.sql.backends.type_resolution import NativeType


def insert_dataframe_batch(
    connection: Any,
    table_name: str,
    batch: pd.DataFrame,
    target_column_types: dict[str, str] | None,
    on_progress: Any = None,
) -> None:
    if len(batch) == 0:
        if on_progress is not None:
            on_progress(0)
        return
    if getattr(connection, "is_native_transport", False):
        columns = list(batch.columns)
        connection.insert(
            table=table_name,
            data=[
                normalize_typed_row(columns, row, target_column_types, native_transport=True)
                for row in batch.itertuples(index=False, name=None)
            ],
            column_names=columns,
            column_type_names=column_type_names(columns, target_column_types),
        )
    else:
        normalized_batch = normalize_typed_batch(batch, target_column_types, native_transport=False)
        connection.insert_df(
            table=table_name,
            df=normalized_batch,
            column_names=list(batch.columns),
        )
    if on_progress is not None:
        on_progress(len(batch))


def normalize_batch(batch: pd.DataFrame) -> pd.DataFrame:
    return _object_dataframe(
        [
            [normalize_scalar(_normalize_nullable_scalar(value)) for value in batch.iloc[:, index]]
            for index in range(len(batch.columns))
        ],
        list(batch.columns),
        batch.index,
    )


def normalize_typed_batch(
    batch: pd.DataFrame,
    column_types: dict[str, str] | None,
    *,
    native_transport: bool | None = None,
) -> pd.DataFrame:
    normalized = normalize_batch(batch)
    if not column_types:
        return normalized
    return _object_dataframe(
        [
            [
                _normalize_typed_scalar(
                    value, column_types.get(column_name, ""), native_transport=native_transport
                )
                for value in normalized.iloc[:, index]
            ]
            for index, column_name in enumerate(normalized.columns)
        ],
        list(normalized.columns),
        normalized.index,
    )


def _object_dataframe(columns: Any, names: Any, index: Any) -> pd.DataFrame:
    frame = pd.DataFrame(
        {
            position: pd.Series(values, index=index, dtype=object)
            for position, values in enumerate(columns)
        },
        index=index,
    )
    frame.columns = names
    return frame


def normalize_row(row: Sequence[Any]) -> tuple[Any, ...]:
    return tuple(normalize_scalar(_normalize_nullable_scalar(value)) for value in row)


def normalize_typed_row(
    columns: Sequence[str],
    row: Sequence[Any],
    column_types: dict[str, str] | None,
    *,
    native_transport: bool | None = None,
) -> tuple[Any, ...]:
    normalized = normalize_row(row)
    if not column_types:
        return normalized
    return tuple(
        _normalize_typed_scalar(
            value, column_types.get(column, ""), native_transport=native_transport
        )
        for column, value in zip(columns, normalized)
    )


def normalize_rows(
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
    column_types: dict[str, str] | None,
    *,
    native_transport: bool | None = None,
) -> list[tuple[Any, ...]]:
    if column_types:
        return [
            normalize_typed_row(columns, row, column_types, native_transport=native_transport)
            for row in rows
        ]
    return [normalize_row(row) for row in rows]


def _normalize_typed_scalar(  # noqa: C901, PLR0911 - exact recursive native types
    value: Any, type_name: str, *, native_transport: bool | None = None
) -> Any:
    if _is_null_like(value):
        return None
    from analytics_toolkit.sql.backends.type_resolution import (  # noqa: PLC0415 - backend registry import cycle
        parse_native_type,
    )

    from .storage_types import logical_storage_type  # noqa: PLC0415 -- logical value shape.

    raw = parse_native_type(type_name).base
    parsed = parse_native_type(logical_storage_type(type_name)).base
    normalized_type = parsed.original
    handled, normalized = _normalize_alias_value(value, raw, native_transport=native_transport)
    if handled:
        return normalized
    if parsed.name in {"ipv4", "ipv6"}:
        from .network_values import (  # noqa: PLC0415 -- schema-driven native network transport.
            convert_network_value,
        )

        return convert_network_value(value, type_name)
    if parsed.name == "array":
        child = raw.arguments[0] if raw.name == "array" else parsed.arguments[0]
        return [
            _normalize_typed_scalar(item, child, native_transport=native_transport)
            for item in value
        ]
    if parsed.name == "tuple":
        return _normalize_tuple_value(value, raw, parsed, native_transport=native_transport)
    if parsed.name == "map":
        children = raw.arguments if raw.name == "map" else parsed.arguments
        pairs = [
            (
                _normalize_typed_scalar(key, children[0], native_transport=native_transport),
                _normalize_typed_scalar(item, children[1], native_transport=native_transport),
            )
            for key, item in value.items()
        ]
        from analytics_toolkit.sql.backends.map_values import (  # noqa: PLC0415 -- retain every native map key.
            converted_map,
        )

        return converted_map(
            pairs, "ch", mapping_input=isinstance(value, dict), key_type=parsed.arguments[0]
        )
    if re.fullmatch(r"U?Int(?:8|16|32|64|128|256)", normalized_type):
        return int(value)
    if parsed.name.startswith("decimal"):
        return Decimal(str(value))
    if parsed.name.startswith("float"):
        return float(value)
    if parsed.name == "string":
        from analytics_toolkit.sql.core.exact_values import (  # noqa: PLC0415 - avoid backend registry import cycles
            typed_text,
        )

        if isinstance(value, (memoryview, bytearray)):
            value = bytes(value)
        elif not isinstance(value, (bytes, str)):
            value = typed_text(value)
    return value


def _normalize_terminal_value(
    value: Any, native: NativeType, *, native_transport: bool
) -> tuple[bool, Any]:
    if native.name == "uuid" and isinstance(value, str):
        return True, UUID(value)
    if native.name in {"enum8", "enum16"}:
        from .value_validation import enum_value_code  # noqa: PLC0415 -- exact native Enum code.

        return True, enum_value_code(value, native.original)
    if native_transport is False and native.name == "string":
        return True, _http_string_bytes(value)
    if native_transport is False and native.name == "fixedstring":
        from .value_validation import (  # noqa: PLC0415 -- declared native byte bound.
            validate_constrained_value,
        )

        normalized = validate_constrained_value(value, native.original)
        packed = normalized.encode("utf-8") if isinstance(normalized, str) else bytes(normalized)
        return True, packed.ljust(int(native.arguments[0]), b"\x00")
    from .client_validation import (  # noqa: PLC0415 -- exact opaque native value policy.
        is_legacy_json_type,
        normalize_legacy_json_value,
    )

    if is_legacy_json_type(native.original):
        import json  # noqa: PLC0415 -- preserve existing native JSON encoder spelling.

        normalized = normalize_legacy_json_value(value, native.original)
        return True, (
            json.dumps(normalized, allow_nan=False) if isinstance(normalized, dict) else normalized
        )
    return False, value


def _http_string_bytes(value: Any) -> bytes | None:
    from analytics_toolkit.sql.core.exact_values import (  # noqa: PLC0415 -- exact explicit String conversions.
        typed_text,
    )

    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value)
    text = value if isinstance(value, str) else typed_text(value)
    return None if text is None else text.encode("utf-8")


def _normalize_alias_value(
    value: Any, raw: NativeType, *, native_transport: bool | None
) -> tuple[bool, Any]:
    from .storage_types import storage_fields  # noqa: PLC0415 -- raw alias children.

    if raw.original.partition("(")[0].strip() == "SimpleAggregateFunction":
        return True, _normalize_typed_scalar(
            value, raw.arguments[1], native_transport=native_transport
        )
    fields = storage_fields(raw.original) if raw.name == "nested" else None
    if fields is not None:
        return True, _normalize_nested_rows(value, fields, native_transport=native_transport)
    return (
        (False, value)
        if native_transport is None
        else _normalize_terminal_value(value, raw, native_transport=native_transport)
    )


def _normalize_tuple_value(
    value: Any, raw: NativeType, logical: NativeType, *, native_transport: bool | None
) -> Any:
    from analytics_toolkit.sql.backends.nested_types import (  # noqa: PLC0415 -- named tuple fields.
        nested_descriptor,
    )

    from .storage_types import storage_fields  # noqa: PLC0415 -- retain raw child codecs.

    fields = storage_fields(raw.original) or nested_descriptor(logical.original)[1]
    if isinstance(value, dict):
        normalized = {
            name: _normalize_typed_scalar(value[name], native, native_transport=native_transport)
            for name, native in fields
        }
        return normalized if native_transport is None else tuple(normalized.values())
    return tuple(
        _normalize_typed_scalar(item, native, native_transport=native_transport)
        for item, (_name, native) in zip(value, fields)
    )


def _normalize_nested_rows(
    value: Any,
    fields: Sequence[tuple[str, str]],
    *,
    native_transport: bool | None,
) -> list[Any]:
    from analytics_toolkit.sql.backends.nested_values import (  # noqa: PLC0415 -- exact named field binding.
        row_items,
    )

    result = []
    for row in value:
        values = tuple(
            _normalize_typed_scalar(item, native, native_transport=native_transport)
            for item, (_name, native) in zip(row_items(row, list(fields)), fields)
        )
        use_mapping = native_transport is False or (
            native_transport is None and isinstance(row, dict)
        )
        result.append(
            dict(zip((name for name, _native in fields), values)) if use_mapping else values
        )
    return result


def normalize_scalar(value: object) -> object:
    from analytics_toolkit.sql.core.exact_values import (  # noqa: PLC0415 - avoid backend registry import cycles
        OrderedMapPairs,
        unwrap_enum,
    )

    value = unwrap_enum(value)
    if isinstance(value, OrderedMapPairs):
        return OrderedMapPairs(
            (
                normalize_scalar(_normalize_nullable_scalar(key)),
                normalize_scalar(_normalize_nullable_scalar(item)),
            )
            for key, item in value
        )
    if isinstance(value, list):
        return [normalize_scalar(_normalize_nullable_scalar(item)) for item in value]
    if isinstance(value, tuple):
        return tuple(normalize_scalar(_normalize_nullable_scalar(item)) for item in value)
    if isinstance(value, dict):
        pairs = [
            (
                normalize_scalar(_normalize_nullable_scalar(key)),
                normalize_scalar(_normalize_nullable_scalar(item)),
            )
            for key, item in value.items()
        ]
        try:
            normalized = dict(pairs)
        except TypeError:
            return OrderedMapPairs(pairs)
        return normalized if len(normalized) == len(pairs) else OrderedMapPairs(pairs)
    return value


def column_type_names(
    columns: Sequence[str],
    column_types: dict[str, str] | None,
) -> list[str] | None:
    if column_types is None:
        return None
    try:
        return [column_types[column_name] for column_name in columns]
    except KeyError as exc:
        msg = f"Missing explicit SQL type for column {exc.args[0]!r}."
        raise ValueError(msg) from exc


def _normalize_nullable_scalar(value: Any) -> Any:
    if _is_null_like(value):
        return None
    return value


def _is_null_like(value: Any) -> bool:
    from analytics_toolkit.sql.core.exact_values import (  # noqa: PLC0415 - avoid backend registry import cycles
        is_null,
    )

    return is_null(value)
