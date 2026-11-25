"""Preflight the selected ClickHouse writer without submitting client values."""

from __future__ import annotations

import datetime as dt
import json
from copy import copy
from dataclasses import dataclass, field, replace
from decimal import Decimal
from hashlib import sha256
from ipaddress import IPv4Address, IPv6Address
from math import isfinite
from numbers import Integral, Real
from struct import pack
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from uuid import UUID

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from analytics_toolkit.sql.backends.value_profiles import ColumnProfile

_SCHEMA_ERROR = (
    "The selected ClickHouse client cannot encode the actual writer schema. "
    "Use a supported client and native type combination."
)
_VALUE_ERROR = "Selected input is incompatible with the selected ClickHouse native encoder."
_JSON_ERROR = "Selected input is incompatible with ClickHouse Object('json') values."
_CONTEXT_ERROR = "The native ClickHouse encoder requires its active connection context."


class ClientWriteValidationError(ValueError):
    analytics_toolkit_sql_retry_safe = False


@dataclass(frozen=True)
class ClientWriteValidation:
    driver: str
    column_types: tuple[tuple[str, str], ...]
    encoded_columns: tuple[str, ...]
    opaque_columns: tuple[str, ...]
    certified_input_count: int | None
    validation_basis: str = "type-capability"
    normalization_types: tuple[tuple[str, str], ...] = ()
    geometry_basis: str = "type-only"
    geometry_digest: str | None = None
    requires_value_encoding: bool = field(default=False, repr=False)
    _domain_verified: bool = field(default=False, init=False, repr=False, compare=False)

    def require_complete(self, expected_count: int) -> None:
        """An opaque or structural codec must not certify an incomplete input scan."""
        if self.validation_basis == "publication-batches-required":
            message = "ClickHouse encoder validation requires the actual publication batches."
            raise ClientWriteValidationError(message)
        if self.validation_basis == "declared-source-domain" and self._domain_verified:
            return
        if self.encoded_columns and self.certified_input_count != expected_count:
            message = "ClickHouse native encoder validation requires the complete frozen input."
            raise ClientWriteValidationError(message)

    def prove_declared_domain(
        self,
        source_schema: Mapping[str, str],
        *,
        intermediate_schema: Mapping[str, str] | None = None,
    ) -> ClientWriteValidation:
        """Certify a total typed conversion without claiming to have scanned values."""
        message = "ClickHouse client domain validation requires complete frozen input."
        if self.opaque_columns or self.requires_value_encoding:
            raise ClientWriteValidationError(message)
        intermediate_types = intermediate_schema or dict(self.normalization_types)
        try:
            compatible = all(
                _declared_client_domain_fits(
                    source_schema.get(name),
                    intermediate_types.get(name, target),
                    target,
                    self.driver,
                )
                for name, target in self.column_types
            )
        except Exception:  # noqa: BLE001 -- deterministic declaration errors may contain private names.
            raise ClientWriteValidationError(message) from None
        if not compatible:
            raise ClientWriteValidationError(message)
        certificate = replace(
            self,
            certified_input_count=None,
            validation_basis="declared-source-domain",
            geometry_basis="declared-source-domain",
            geometry_digest=None,
        )
        object.__setattr__(certificate, "_domain_verified", True)
        return certificate


@dataclass(frozen=True)
class _JsonObjectPairs:
    pairs: list[tuple[str, Any]]


def client_write_driver(connection: Any) -> str:
    return "native" if getattr(connection, "is_native_transport", False) is True else "http"


def effective_client_write_schema(
    connection: Any,
    incoming_schema: Mapping[str, str],
    physical_schema: Mapping[str, str] | None = None,
    *,
    writer_kind: str = "dataframe",
) -> dict[str, str]:
    """Native receives a server sample block; HTTP rows may supply Native headers."""
    if writer_kind not in {"dataframe", "rows"}:
        message = "ClickHouse client writer kind must be dataframe or rows."
        raise ClientWriteValidationError(message)
    supplied_header = writer_kind == "rows" and client_write_driver(connection) == "http"
    physical = None if supplied_header else physical_schema
    return {
        name: str(physical.get(name, native) if physical is not None else native)
        for name, native in incoming_schema.items()
    }


def is_legacy_json_type(native_type: str) -> bool:
    from analytics_toolkit.sql.backends.type_resolution import (  # noqa: PLC0415 -- native grammar cycle.
        parse_native_type,
    )

    native = parse_native_type(native_type).base
    return native.name == "object" and native.arguments == ("'json'",)


def normalize_legacy_json_value(value: Any, native_type: str) -> Any:
    """Validate the owned outgoing copy using existing native null normalization."""
    from analytics_toolkit.sql.core.exact_values import (  # noqa: PLC0415 -- exact scalar registry cycle.
        is_null,
        unwrap_enum,
    )

    if not is_legacy_json_type(native_type):
        raise ClientWriteValidationError(_JSON_ERROR)
    value = unwrap_enum(value)
    if is_null(value):
        return None
    if not isinstance(value, (dict, str)):
        raise ClientWriteValidationError(_JSON_ERROR)
    try:
        if isinstance(value, dict):
            # Preserve existing native JSON/null semantics.
            from .insert import (  # noqa: PLC0415
                normalize_scalar,
            )

            value = normalize_scalar(value)
        encoded = value if isinstance(value, str) else json.dumps(value, allow_nan=False)
        parsed = json.loads(
            encoded,
            parse_constant=_invalid_json_constant,
            object_pairs_hook=_JsonObjectPairs,
        )
        _validate_json_tree(parsed)
    except (TypeError, ValueError, OverflowError, RecursionError):
        raise ClientWriteValidationError(_JSON_ERROR) from None
    if not isinstance(parsed, _JsonObjectPairs):
        raise ClientWriteValidationError(_JSON_ERROR)
    return value


def validate_legacy_json_value(value: Any, native_type: str) -> None:
    normalize_legacy_json_value(value, native_type)


def _invalid_json_constant(_value: str) -> Any:
    raise ClientWriteValidationError(_JSON_ERROR)


def _validate_json_tree(value: Any) -> None:
    if isinstance(value, _JsonObjectPairs):
        for key, child in value.pairs:
            key.encode("utf-8")
            _validate_json_tree(child)
    elif isinstance(value, list):
        for child in value:
            _validate_json_tree(child)
    elif isinstance(value, str):
        value.encode("utf-8")
    elif isinstance(value, float) and not isfinite(value):
        raise ClientWriteValidationError(_JSON_ERROR)


def observe_legacy_json_value(profile: ColumnProfile, value: Any) -> None:
    if profile.source_backend != "ch" or not is_legacy_json_type(profile.native_type or ""):
        return
    try:
        validate_legacy_json_value(value, profile.native_type or "")
    except ValueError:
        profile.native_constraints_valid = False


def validate_legacy_json_profile(profile: ColumnProfile, native_type: str) -> bool:
    if not is_legacy_json_type(native_type):
        return False
    if (
        not profile.native_constraints_valid
        or not profile.utf8_valid
        or not profile.families <= {"map", "string"}
    ):
        raise ClientWriteValidationError(_JSON_ERROR)
    return True


def _logical_type(native_type: str) -> str:
    from .storage_types import logical_storage_type  # noqa: PLC0415 -- CH structural domain.

    return logical_storage_type(native_type)


def _serializer_shape(native_type: str, driver: str) -> tuple[Any, ...]:
    # Selected codec and native grammar cycle.
    from analytics_toolkit.sql.backends.type_resolution import (  # noqa: PLC0415
        parse_native_type,
    )

    # Retain raw Nested transport identity.
    from .storage_types import (  # noqa: PLC0415
        storage_fields,
    )

    native = parse_native_type(native_type).base
    if native.name == "simpleaggregatefunction":
        return _serializer_shape(native.arguments[1], driver)
    if native.name in {"array", "map"}:
        return native.name, tuple(_serializer_shape(child, driver) for child in native.arguments)
    fields = storage_fields(native.original)
    if fields is not None:
        return _named_serializer_shape(native.name, fields, driver)
    logical = _logical_type(native.original)
    if logical != native.original:
        return _serializer_shape(logical, driver)
    return _scalar_serializer_shape(native.name)


def _named_serializer_shape(
    name: str, fields: tuple[tuple[str, str], ...], driver: str
) -> tuple[Any, ...]:
    children = tuple(_serializer_shape(child, driver) for _name, child in fields)
    if name == "nested":
        if driver == "http":
            return "nested", tuple(
                (field, child) for (field, _native), child in zip(fields, children)
            )
        return "array", (("tuple", children),)
    return "tuple", children


def _scalar_serializer_shape(name: str) -> tuple[str]:
    width = name[4:] if name.startswith("uint") else name[3:] if name.startswith("int") else ""
    if name.startswith("decimal") or width.isdigit():
        return ("exact-number",)
    if name in {"float32", "float64"}:
        return ("float",)
    if name in {"date", "date32"}:
        return ("date",)
    if name in {"datetime", "datetime64"}:
        return ("timestamp",)
    return (name,)


def _declared_client_domain_fits(
    source: str | None, intermediate: str, target: str, driver: str
) -> bool:
    # Selected codec and native grammar cycle.
    from analytics_toolkit.sql.backends.type_resolution import (  # noqa: PLC0415
        declared_domain_fits,
    )

    return bool(
        source is not None
        and _serializer_shape(intermediate, driver) == _serializer_shape(target, driver)
        and declared_domain_fits(source, intermediate, "ch", source_backend="ch")
        and declared_domain_fits(intermediate, target, "ch", source_backend="ch")
    )


def _contains_legacy_json(native_type: str) -> bool:
    from analytics_toolkit.sql.backends.nested_types import (  # noqa: PLC0415 -- recursive native domain.
        nested_descriptor,
    )

    logical = _logical_type(native_type)
    if is_legacy_json_type(logical):
        return True
    return any(_contains_legacy_json(child) for _name, child in nested_descriptor(logical)[1])


def requires_client_write_validation(schema: Mapping[str, str]) -> bool:
    return any(
        _logical_type(native) != native or _contains_legacy_json(native)
        for native in schema.values()
    )


def _detached_native_context() -> Any:
    from clickhouse_driver.context import Context  # noqa: PLC0415 -- selected optional SDK.

    context = Context()
    context.settings = {}
    context.client_settings = {
        "use_numpy": False,
        "strings_encoding": "utf-8",
        "strings_as_bytes": False,
    }
    # This context constructs columns only. It can never certify encoded values.
    context.server_info = SimpleNamespace(used_revision=0, get_timezone=lambda: "UTC")
    return context


def _native_client_chain(connection: Any) -> Iterable[Any]:
    current = connection
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        attributes = vars(current) if hasattr(current, "__dict__") else {}
        current = attributes.get("_target", attributes.get("_client"))


def _active_native_context(connection: Any) -> Any:
    for current in _native_client_chain(connection):
        attributes = vars(current) if hasattr(current, "__dict__") else {}
        context = attributes.get("context")
        if context is not None:
            return context
        native_connection = attributes.get("connection")
        if (
            native_connection is not None
            and getattr(native_connection, "context", None) is not None
        ):
            return native_connection.context
    return None


def _native_base_settings(connection: Any) -> tuple[dict[str, Any], dict[str, Any]] | None:
    from clickhouse_driver import Client  # noqa: PLC0415 -- actual selected native client identity.

    for current in _native_client_chain(connection):
        if isinstance(current, Client):
            return dict(current.client_settings), dict(current.settings)
    return None


def _native_column(native_type: str, context: Any) -> Any:
    # Import the selected optional SDK only for native execution.
    from clickhouse_driver.columns.service import (  # noqa: PLC0415
        get_column_by_spec,
    )

    from .native_codec import _prepare_column  # noqa: PLC0415 -- owned exact writer factory.

    return _prepare_column(
        get_column_by_spec(native_type, {"context": context, "types_check": False}, use_numpy=False)
    )


def _http_column(native_type: str, connection: Any) -> Any:
    # Import the selected optional SDK only for HTTP execution.
    from clickhouse_connect.datatypes.registry import (  # noqa: PLC0415
        get_from_name,
    )

    from .http_codec import exact_http_type  # noqa: PLC0415 -- owned exact writer factory.

    _require_http_registered_types(native_type)
    column = exact_http_type(
        get_from_name(native_type),
        default_timezone=getattr(connection, "effective_write_timezone", None),
    )
    _require_http_writer(column)
    return column


def _require_http_registered_types(native_type: str) -> None:
    """Reject unsupported names before the SDK logs their literal type arguments."""
    # Use the selected SDK registry and its actual type grammar.
    from clickhouse_connect.datatypes.base import (  # noqa: PLC0415
        type_map,
    )

    # Load only the selected optional SDK.
    from clickhouse_connect.datatypes.registry import (  # noqa: PLC0415
        parse_name,
    )

    # Selected codec and native grammar cycle.
    from analytics_toolkit.sql.backends.type_resolution import (  # noqa: PLC0415
        parse_native_type,
    )

    from .storage_types import storage_fields  # noqa: PLC0415 -- named tuple/nested child grammar.

    base, _name, _definition = parse_name(native_type)
    if base not in type_map:
        raise ClientWriteValidationError(_SCHEMA_ERROR)
    native = parse_native_type(native_type).base
    if native.name == "array":
        children = native.arguments[:1]
    elif native.name == "map":
        children = native.arguments[:2]
    elif native.name == "simpleaggregatefunction":
        children = native.arguments[1:2]
    else:
        children = tuple(child for _field, child in storage_fields(native_type) or ())
    for child in children:
        _require_http_registered_types(child)


def _require_http_writer(column: Any) -> None:
    # Construction alone does not certify SDK types marked as unsupported.
    # Load only the selected optional SDK.
    from clickhouse_connect.datatypes.base import (  # noqa: PLC0415
        UnsupportedType,
    )

    if isinstance(column, UnsupportedType):
        raise ClientWriteValidationError(_SCHEMA_ERROR)
    for attribute in ("element_type", "key_type", "value_type", "tuple_array"):
        child = getattr(column, attribute, None)
        if child is not None:
            _require_http_writer(child)
    for child in getattr(column, "element_types", ()):
        _require_http_writer(child)


class _DiscardSocket:
    def sendall(self, _payload: Any) -> None:
        """A buffered SDK writer emits into this run-local sink, never a socket."""


def _owned_value(value: Any) -> Any:
    # Selected codec and native grammar cycle.
    from analytics_toolkit.sql.core.exact_values import (  # noqa: PLC0415
        decode_value,
        encode_value,
    )

    return decode_value(encode_value(value))


def _encode_native_values(column: Any, values: Sequence[Any]) -> None:
    # Exercise the real binary encoder against a local sink.
    from clickhouse_driver.bufferedwriter import (  # noqa: PLC0415
        BufferedSocketWriter,
    )

    writer = BufferedSocketWriter(_DiscardSocket(), 65536)
    values = list(values)
    if getattr(column, "prefix_needs_items", False):
        column.write_state_prefix(writer, values)
    else:
        column.write_state_prefix(writer)
    column.write_data(values, writer)
    writer.flush()


def _object_shapes(
    value: Any,
    native_type: str,
    path: tuple[int, ...] = (),
    *,
    physical: bool = False,
) -> Iterable[tuple[tuple[int, ...], str]]:
    # Selected codec and native grammar cycle.
    from analytics_toolkit.sql.backends.nested_types import (  # noqa: PLC0415
        nested_descriptor,
    )

    # Traverse fields in their declared order while preserving exact SQL NULL.
    from analytics_toolkit.sql.backends.nested_values import (  # noqa: PLC0415
        row_items,
    )

    # Selected codec and native grammar cycle.
    from analytics_toolkit.sql.core.exact_values import (  # noqa: PLC0415
        is_null,
    )

    logical = _logical_type(native_type)
    if is_null(value):
        from .value_validation import _permits_null  # noqa: PLC0415 -- physical SQL nullability.

        if physical and not _permits_null(logical):
            raise ClientWriteValidationError(_VALUE_ERROR)
        return
    if is_legacy_json_type(logical):
        validate_legacy_json_value(value, logical)
        yield path, "text" if isinstance(value, str) else "object"
        return
    kind, fields = nested_descriptor(logical)
    if kind == "array":
        for item in value:
            yield from _object_shapes(item, fields[0][1], (*path, 0), physical=physical)
    elif kind in {"tuple", "row"}:
        for index, (item, (_name, native)) in enumerate(zip(row_items(value, fields), fields)):
            yield from _object_shapes(item, native, (*path, index), physical=physical)
    elif kind == "map":
        for key, item in value.items():
            yield from _object_shapes(key, fields[0][1], (*path, 0), physical=physical)
            yield from _object_shapes(item, fields[1][1], (*path, 1), physical=physical)


@dataclass
class _ClientCodecs:
    connection: Any
    driver: str
    schema: tuple[tuple[str, str], ...]
    normalization: dict[str, str]
    encoded: tuple[str, ...]
    opaque: tuple[str, ...]
    active_context: Any
    requires_value_encoding: bool = False


def _conversion_columns(
    schema: tuple[tuple[str, str], ...], normalization: Mapping[str, str]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    opaque = tuple(
        name
        for name, native in schema
        if _contains_legacy_json(native) or _contains_legacy_json(normalization[name])
    )
    encoded = tuple(
        name
        for name, native in schema
        if _logical_type(native) != native
        or _logical_type(normalization[name]) != normalization[name]
        or native != normalization[name]
        or name in opaque
    )
    return encoded, opaque


def _client_codecs(
    connection: Any,
    schema: tuple[tuple[str, str], ...],
    normalization_schema: Mapping[str, str] | None,
    *,
    writer_kind: str,
    prospective_schema: bool,
) -> _ClientCodecs:
    normalization = dict(normalization_schema or schema)
    if not all(name in normalization for name, _native in schema):
        raise ClientWriteValidationError(_SCHEMA_ERROR)
    driver = client_write_driver(connection)
    try:
        encoded, opaque = _conversion_columns(schema, normalization)
    except Exception:  # noqa: BLE001 - sanitize schema failures before publishing selected values.
        raise ClientWriteValidationError(_SCHEMA_ERROR) from None
    active = _active_native_context(connection) if driver == "native" else None
    requires_values = False
    try:
        context = active or (_detached_native_context() if driver == "native" else None)
        if driver == "native":
            from .string_values import (  # noqa: PLC0415 - selected codec and native grammar cycle.
                contains_string_type,
                requires_string_value_encoding,
            )

            string_columns = tuple(name for name, native in schema if contains_string_type(native))
            requires_values = bool(string_columns) and requires_string_value_encoding(
                connection, context
            )
            if requires_values:
                encoded = tuple(
                    name for name, _native in schema if name in encoded or name in string_columns
                )
        for _name, native in schema:
            if driver == "native":
                _native_column(native, context)
            else:
                _http_column(native, connection)
    except Exception:  # noqa: BLE001 - selected SDK factories expose different exception classes.
        if not prospective_schema or (driver == "http" and writer_kind == "rows"):
            raise ClientWriteValidationError(_SCHEMA_ERROR) from None
    else:
        return _ClientCodecs(
            connection, driver, schema, normalization, encoded, opaque, active, requires_values
        )
    # Resolve aliases using destination metadata only after a factory failure.
    from .client_schema import (  # noqa: PLC0415
        resolve_prospective_schema,
    )

    canonical = resolve_prospective_schema(connection, dict(schema))
    return _client_codecs(
        connection,
        tuple(canonical.items()),
        normalization,
        writer_kind=writer_kind,
        prospective_schema=False,
    )


def _prepare_row(codecs: _ClientCodecs, row: Sequence[Any]) -> tuple[Any, ...]:
    # Frozen-input scans remain cancellable before publication.
    from analytics_toolkit.sql.execution.cancellation import (  # noqa: PLC0415
        raise_if_cancelled,
    )

    from .insert import normalize_typed_row  # noqa: PLC0415 -- actual publication normalizer.

    raise_if_cancelled()
    if len(row) != len(codecs.schema):
        message = "ClickHouse encoder input does not match the actual writer schema."
        raise ClientWriteValidationError(message)
    for (name, _native), value in zip(codecs.schema, row):
        for _path, _shape in _object_shapes(value, codecs.normalization[name]):
            pass
    prepared = normalize_typed_row(
        [name for name, _native in codecs.schema],
        [_owned_value(value) for value in row],
        codecs.normalization,
        native_transport=codecs.driver == "native",
    )
    for (_name, native), value in zip(codecs.schema, prepared):
        for _path, _shape in _object_shapes(value, native, physical=True):
            pass
    return prepared


def _prepared_batch(codecs: _ClientCodecs, batch: Any) -> list[tuple[Any, ...]]:
    import pandas as pd  # noqa: PLC0415 -- dataframe input is already in memory.

    from .insert import normalize_typed_batch  # noqa: PLC0415 -- actual dataframe publication.

    if isinstance(batch, pd.DataFrame):
        names = [name for name, _native in codecs.schema]
        if len(batch.columns) != len(names) or set(batch.columns) != set(names):
            message = "ClickHouse encoder dataframe does not match the actual writer schema."
            raise ClientWriteValidationError(message)
        batch = normalize_typed_batch(
            batch, codecs.normalization, native_transport=codecs.driver == "native"
        ).loc[:, names]
        return [_prepare_row(codecs, row) for row in batch.itertuples(index=False, name=None)]
    return [_prepare_row(codecs, row) for row in batch]


def _encode_http_batch(codecs: _ClientCodecs, rows: Sequence[Sequence[Any]]) -> list[int]:
    # Follow the real SDK's internal block geometry.
    from clickhouse_connect.driver.insert import (  # noqa: PLC0415
        InsertContext,
    )

    columns = [_http_column(native, codecs.connection) for _name, native in codecs.schema]
    context = InsertContext(
        "_atk_codec_validation",
        [name for name, _native in codecs.schema],
        columns,
        data=rows,
        column_oriented=False,
    )
    counts = []
    for block in context.next_block():
        counts.append(block.row_count)
        for name, column, values in zip(block.column_names, block.column_types, block.column_data):
            if name in codecs.encoded:
                context.start_column(name)
                column.write_column(values, bytearray(), context)
    return counts


def _native_batch_context(
    codecs: _ClientCodecs, rows: Sequence[Sequence[Any]]
) -> tuple[Any, Sequence[Sequence[Any]]]:
    from .native_client import (  # noqa: PLC0415 -- exact preprocessing also used by publication.
        _prepare_binary_string_insert,
    )

    if codecs.active_context is None:
        raise ClientWriteValidationError(_CONTEXT_ERROR)
    context = copy(codecs.active_context)
    base_settings = _native_base_settings(codecs.connection)
    if base_settings is not None:
        context.client_settings, context.settings = base_settings
    rows, settings = _prepare_binary_string_insert(
        SimpleNamespace(client_settings=context.client_settings),
        rows,
        [codecs.normalization[name] for name, _native in codecs.schema],
    )
    context.client_settings = {**context.client_settings, **(settings or {})}
    return context, rows


def _encode_native_batch(codecs: _ClientCodecs, rows: Sequence[Sequence[Any]]) -> list[int]:
    # Match the native driver's default block geometry.
    from clickhouse_driver.defines import (  # noqa: PLC0415
        DEFAULT_INSERT_BLOCK_SIZE,
    )

    context, rows = _native_batch_context(codecs, rows)
    size = context.client_settings.get("insert_block_size", DEFAULT_INSERT_BLOCK_SIZE)
    if not isinstance(size, int) or size <= 0:
        message = "The native ClickHouse insert block size must be positive."
        raise ClientWriteValidationError(message)
    counts = []
    for start in range(0, len(rows), size):
        block = rows[start : start + size]
        counts.append(len(block))
        for index, (name, native) in enumerate(codecs.schema):
            if name in codecs.encoded:
                _encode_native_values(
                    _native_column(native, context), [row[index] for row in block]
                )
    return counts


def _encode_prepared_batch(codecs: _ClientCodecs, rows: Sequence[Sequence[Any]]) -> list[int]:
    if not rows:
        return []
    return (
        _encode_native_batch(codecs, rows)
        if codecs.driver == "native"
        else _encode_http_batch(codecs, rows)
    )


def _scalar_form(value: Any) -> str:
    # Temporal carriers preserve raw ticks before selecting an encoder form.
    # Selected codec and native grammar cycle.
    from analytics_toolkit.sql.core.exact_values import (  # noqa: PLC0415
        ExactTemporal,
    )

    for kind, family in (
        (str, "text"),
        (bytes, "bytes"),
        (Integral, "integer"),
        (Real, "floating"),
        (Decimal, "decimal"),
        (UUID, "uuid"),
        (dt.datetime, "timestamp"),
        (dt.date, "date"),
        (IPv4Address, "ipv4"),
        (IPv6Address, "ipv6"),
        (ExactTemporal, "exact-temporal"),
    ):
        if isinstance(value, kind):
            return family
    return "opaque"


def _encoding_forms(
    value: Any, native_type: str, path: tuple[int, ...] = ()
) -> Iterable[tuple[tuple[int, ...], str]]:
    # Compare runtime forms at logical leaves in declared field order.
    # Selected codec and native grammar cycle.
    from analytics_toolkit.sql.backends.nested_types import (  # noqa: PLC0415
        nested_descriptor,
    )

    # Selected codec and native grammar cycle.
    from analytics_toolkit.sql.backends.nested_values import (  # noqa: PLC0415
        row_items,
    )

    # Selected codec and native grammar cycle.
    from analytics_toolkit.sql.core.exact_values import (  # noqa: PLC0415
        is_null,
    )

    if is_null(value):
        return
    logical = _logical_type(native_type)
    if is_legacy_json_type(logical):
        yield path, _scalar_form(value)
        return
    kind, fields = nested_descriptor(logical)
    if kind == "array":
        for item in value:
            yield from _encoding_forms(item, fields[0][1], (*path, 0))
    elif kind in {"tuple", "row"}:
        for index, (item, (_name, native)) in enumerate(zip(row_items(value, fields), fields)):
            yield from _encoding_forms(item, native, (*path, index))
    elif kind == "map":
        for key, item in value.items():
            yield from _encoding_forms(key, fields[0][1], (*path, 0))
            yield from _encoding_forms(item, fields[1][1], (*path, 1))
    else:
        yield path, _scalar_form(value)


def _scan_rows(codecs: _ClientCodecs, rows: Iterable[Sequence[Any]]) -> tuple[int, bool]:
    count = 0
    forms: dict[tuple[int, tuple[int, ...]], str] = {}
    needs_geometry = False
    for row in rows:
        try:
            prepared = _prepare_row(codecs, row)
            _encode_prepared_batch(codecs, [prepared])
            if codecs.driver == "native":
                context, _native_rows = _native_batch_context(codecs, [prepared])
                mode = str(bool(context.client_settings.get("strings_as_bytes", False)))
                mode_key = -1, ()
                needs_geometry |= mode_key in forms and forms[mode_key] != mode
                forms[mode_key] = mode
            for index, ((_name, native), value) in enumerate(zip(codecs.schema, prepared)):
                for path, form in _encoding_forms(value, native):
                    key = index, path
                    needs_geometry |= form == "opaque" or (key in forms and forms[key] != form)
                    forms[key] = form
        except ClientWriteValidationError:
            raise
        except Exception:  # noqa: BLE001 - raw encoder errors must not expose selected values.
            raise ClientWriteValidationError(_VALUE_ERROR) from None
        count += 1
    return count, needs_geometry


def _scan_batches(codecs: _ClientCodecs, batches: Iterable[Any]) -> tuple[int, str]:
    count = 0
    digest = sha256(b"ATK-CLIENT-BATCHES\x01")
    for batch in batches:
        try:
            prepared = _prepared_batch(codecs, batch)
            digest.update(b"B" + pack(">Q", len(prepared)))
            for block_count in _encode_prepared_batch(codecs, prepared):
                digest.update(b"I" + pack(">Q", block_count))
            digest.update(b"E")
        except ClientWriteValidationError:
            raise
        except Exception:  # noqa: BLE001 - raw encoder errors must not expose selected values.
            raise ClientWriteValidationError(_VALUE_ERROR) from None
        count += len(prepared)
    return count, digest.hexdigest()


def validate_client_write_capabilities(  # noqa: PLR0913 -- internal selected-writer/frozen-input contract.
    connection: Any,
    actual_writer_schema: Mapping[str, str],
    *,
    rows: Iterable[Sequence[Any]] | None = None,
    normalization_schema: Mapping[str, str] | None = None,
    batches: Iterable[Any] | None = None,
    writer_kind: str = "dataframe",
    prospective_schema: bool = False,
) -> ClientWriteValidation:
    """Construct selected codecs and certify frozen values for aliases/opaque JSON."""
    if rows is not None and batches is not None:
        message = "Provide frozen rows or publication batches, not both."
        raise ClientWriteValidationError(message)
    if writer_kind not in {"dataframe", "rows"}:
        message = "ClickHouse client writer kind must be dataframe or rows."
        raise ClientWriteValidationError(message)
    codecs = _client_codecs(
        connection,
        tuple(actual_writer_schema.items()),
        normalization_schema,
        writer_kind=writer_kind,
        prospective_schema=prospective_schema,
    )
    count = None
    basis, geometry, geometry_digest = "type-capability", "type-only", None
    if codecs.encoded and batches is not None:
        count, geometry_digest = _scan_batches(codecs, batches)
        basis, geometry = "encoded-input", "publication-batches"
    elif codecs.encoded and rows is not None:
        count, needs_geometry = _scan_rows(codecs, rows)
        basis = "publication-batches-required" if needs_geometry else "encoded-input"
        geometry = "publication-batches-required" if needs_geometry else "canonical-forms"
    return ClientWriteValidation(
        codecs.driver,
        codecs.schema,
        codecs.encoded,
        codecs.opaque,
        count,
        basis,
        tuple(codecs.normalization.items()),
        geometry,
        geometry_digest,
        codecs.requires_value_encoding,
    )
