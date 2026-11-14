"""Connection-owned exact Trino result mapping over the native paged protocol."""

from __future__ import annotations

import base64
import importlib
import inspect
import json
import logging
from contextlib import suppress
from dataclasses import dataclass
from functools import lru_cache
from http import HTTPStatus
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from collections.abc import Sequence

_VALUE_MAPPER = "_create_value_mapper"
_REQUEST_FACTORY = "_create_request"
_LOGGER = logging.getLogger(__name__)


def requires_value_certification(self: Any, native_types: Sequence[str]) -> bool:
    """Freeze source families whose JSON representation needs per-value checks."""
    del self
    return any(_requires_value_certification(native_type) for native_type in native_types)


def _requires_value_certification(native_type: str) -> bool:
    from analytics_toolkit.sql.backends.nested_types import nested_descriptor  # noqa: PLC0415
    from analytics_toolkit.sql.backends.type_resolution import parse_native_type  # noqa: PLC0415

    if parse_native_type(native_type).base.name in {
        "date",
        "timestamp",
        "timestamp with time zone",
        "timestamp without time zone",
    }:
        # SQL calendar domains exceed Python's calendar range. Zoned timestamps
        # can additionally omit a fold discriminator. Metadata cannot certify
        # complete selected input without validating every calendar value.
        return True
    kind, fields = nested_descriptor(native_type)
    if kind == "map" and fields and nested_descriptor(fields[0][1])[0]:
        # Native JSON object keys cannot certify composite SQL key shapes before
        # their actual carrier has passed the exact reader's validation.
        return True
    return any(_requires_value_certification(child) for _name, child in fields)


class _ExactReadError(ValueError):
    """An actionable decoding error whose message never contains cell values."""


@dataclass(frozen=True)
class _SdkInterface:
    raw_flag: str
    raw_value: bool
    supports_encoding: bool
    mapper_factory: Any
    data_error: Any


@lru_cache(maxsize=1)
def _sdk_interface() -> _SdkInterface:
    dbapi = importlib.import_module("trino.dbapi")
    parameters = inspect.signature(dbapi.Connection).parameters
    if "legacy_primitive_types" in parameters:
        flag, raw = "legacy_primitive_types", True
    elif "experimental_python_types" in parameters:
        flag, raw = "experimental_python_types", False
    else:
        message = "This Trino driver does not expose a supported exact result-mapping interface."
        raise RuntimeError(message)
    try:
        mapping = importlib.import_module("trino.mapper")
    except ModuleNotFoundError as exc:
        if exc.name != "trino.mapper":
            raise
        mapping = importlib.import_module("trino.client")
    factory = getattr(mapping, "RowMapperFactory", None)
    if factory is None or not callable(getattr(factory, _VALUE_MAPPER, None)):
        message = "This Trino driver does not expose a supported native value mapper."
        raise RuntimeError(message)
    data_error = importlib.import_module("trino.exceptions").TrinoDataError
    return _SdkInterface(flag, raw, "encoding" in parameters, factory, data_error)


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            message = (
                "Exact Trino reads cannot preserve duplicate JSON object keys. "
                "Use unique source map keys; project zoned timestamp keys to UTC in source SQL."
            )
            raise _ExactReadError(message)
        result[key] = value
    return result


class _ExactRequest:
    def __init__(self, request: Any) -> None:
        self._request = request

    def __getattr__(self, name: str) -> Any:
        return getattr(self._request, name)

    def process(self, response: Any) -> Any:
        # Older SDKs call Response.json(), newer ones parse Response.text. Check
        # the same raw JSON before either implementation can coerce map keys.
        rejected: _ExactReadError | None = None
        if response.ok:
            response.encoding = "utf-8"
            if response.text.strip():
                try:
                    json.loads(response.text, object_pairs_hook=_unique_json_object)
                except _ExactReadError as exc:
                    rejected = exc
        # Native status processing records the acknowledged cancellation URI.
        # It does not map or publish cells, including rejected raw map entries.
        status = self._request.process(response)
        if rejected is None and isinstance(status.rows, dict):
            message = (
                "Exact Trino reads require direct JSON row pages. "
                "Encoded segments were returned despite requesting the direct protocol."
            )
            rejected = _ExactReadError(message)
        if rejected is not None:
            self._cancel_rejected(status)
            raise rejected from None
        return status

    def _cancel_rejected(self, status: Any) -> None:
        if status.next_uri is None:
            return
        cancelled = False
        with suppress(Exception):
            response = self._request.delete(status.next_uri)
            if response.status_code != HTTPStatus.NO_CONTENT:
                self._request.raise_response_error(response)
            cancelled = True
        if not cancelled:
            _LOGGER.warning(
                "Rejected Trino query cleanup is incomplete; preserving the decode error."
            )


class _TemporalMapper:
    def __init__(self, precision: int, *, with_timezone: bool, clock: bool) -> None:
        self._precision = precision
        self._with_timezone = with_timezone
        self._clock = clock

    def map(self, value: Any) -> Any:
        from .exact_temporal_reads import decode_time, decode_timestamp  # noqa: PLC0415

        decode = decode_time if self._clock else decode_timestamp
        try:
            return decode(value, self._precision, with_timezone=self._with_timezone)
        except ValueError as exc:
            raise _ExactReadError(str(exc)) from None


class _BinaryMapper:
    def map(self, value: Any) -> bytes | None:
        if value is None:
            return None
        if not isinstance(value, str):
            message = "Trino VARBINARY response must be a base64 string."
            raise _ExactReadError(message)
        try:
            return base64.b64decode(value, validate=True)
        except ValueError:
            message = "Trino VARBINARY response has an invalid base64 representation."
            raise _ExactReadError(message) from None


class _MapMapper:
    def __init__(self, keys: Any, values: Any) -> None:
        self._keys = keys
        self._values = values

    def map(self, value: Any) -> Any:
        if value is None:
            return None
        result = {}
        for raw_key, raw_value in value.items():
            key = self._keys.map(raw_key)
            try:
                duplicate = key in result
            except TypeError:
                message = (
                    "Trino result map keys require a lossless, hashable Python representation."
                )
                raise _ExactReadError(message) from None
            if duplicate:
                message = (
                    "Trino result map keys collide after native decoding; "
                    "no entries were discarded."
                )
                raise _ExactReadError(message)
            result[key] = self._values.map(raw_value)
        return result


def _mapper_factory() -> Any:
    base = _sdk_interface().mapper_factory

    def create_value_mapper(self: Any, signature: dict[str, Any]) -> Any:
        native = signature["rawType"]
        if native in {"timestamp", "timestamp with time zone"}:
            precision = int(self._get_precision(signature))
            return _TemporalMapper(precision, with_timezone="with time zone" in native, clock=False)
        if native in {"time", "time with time zone"}:
            precision = int(self._get_precision(signature))
            return _TemporalMapper(precision, with_timezone="with time zone" in native, clock=True)
        if native == "varbinary":
            return _BinaryMapper()
        if native == "map":
            keys, values = (argument["value"] for argument in signature["arguments"])
            return _MapMapper(self._create_value_mapper(keys), self._create_value_mapper(values))
        return getattr(base, _VALUE_MAPPER)(self, signature)

    # Only this factory instance's subclass changes recursion. SDK classes and
    # process-wide mapper functions remain untouched for every other connection.
    owned = type("ExactTrinoMapperFactory", (base,), {_VALUE_MAPPER: create_value_mapper})
    return owned()


class _RowMapper:
    def __init__(self, columns: list[dict[str, Any]]) -> None:
        factory = _mapper_factory()
        create = getattr(factory, _VALUE_MAPPER)
        self._mappers = [create(column["typeSignature"]) for column in columns]

    def map(self, rows: list[Any]) -> list[list[Any]]:
        mapped = []
        for row in rows:
            if len(row) != len(self._mappers):
                message = "Trino result row width differs from its native column metadata."
                raise _ExactReadError(message)
            try:
                mapped.append([mapper.map(value) for mapper, value in zip(self._mappers, row)])
            except _ExactReadError:
                raise
            except (TypeError, ValueError, OverflowError, _sdk_interface().data_error):
                message = "A Trino result value is incompatible with its declared native type."
                raise _ExactReadError(message) from None
        return mapped


class _ExactCursor:
    def __init__(self, cursor: Any) -> None:
        self._cursor = cursor
        self._mapper: _RowMapper | None = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cursor, name)

    @property
    def arraysize(self) -> int:
        return int(self._cursor.arraysize)

    @arraysize.setter
    def arraysize(self, value: int) -> None:
        self._cursor.arraysize = value

    def __enter__(self) -> _ExactCursor:  # noqa: PYI034 -- Python 3.8 lacks typing.Self.
        self._cursor.__enter__()
        return self

    def __exit__(self, *args: object) -> Any:
        return self._cursor.__exit__(*args)

    def __iter__(self) -> _ExactCursor:
        return self

    def __next__(self) -> list[Any]:
        row = self.fetchone()
        if row is None:
            raise StopIteration
        return row

    def execute(self, *args: Any, **kwargs: Any) -> _ExactCursor:
        self._mapper = None
        self._cursor.execute(*args, **kwargs)
        return self

    def executemany(self, *args: Any, **kwargs: Any) -> Any:
        self._mapper = None
        result = self._cursor.executemany(*args, **kwargs)
        return self if result is self._cursor else result

    def _map(self, rows: list[Any]) -> list[list[Any]]:
        if not rows:
            return []
        if self._mapper is None:
            query = getattr(self._cursor, "_query", None)
            columns = getattr(query, "columns", None)
            if columns is None:
                message = (
                    "Exact Trino reads require native result type signatures before row decoding."
                )
                raise _ExactReadError(message)
            self._mapper = _RowMapper(columns)
        return self._mapper.map(rows)

    def fetchone(self) -> list[Any] | None:
        row = self._cursor.fetchone()
        return None if row is None else self._map([row])[0]

    def fetchmany(self, *args: Any, **kwargs: Any) -> list[list[Any]]:
        return self._map(self._cursor.fetchmany(*args, **kwargs))

    def fetchall(self) -> list[list[Any]]:
        return self._map(self._cursor.fetchall())


def connect_exact(connect: Callable[..., Any], **kwargs: Any) -> Any:
    """Keep the native connection while owning all result decoding locally."""
    interface = _sdk_interface()
    if kwargs.get("encoding") is not None:
        message = "Exact Trino reads require direct JSON; remove the explicit spooling encoding."
        raise ValueError(message)
    if interface.supports_encoding:
        kwargs["encoding"] = None
    else:
        kwargs.pop("encoding", None)
    kwargs[interface.raw_flag] = interface.raw_value
    connection = connect(**kwargs)
    request_factory = getattr(connection, _REQUEST_FACTORY, None)
    cursor_factory = getattr(connection, "cursor", None)
    if not callable(request_factory) or not callable(cursor_factory):
        message = "This Trino connection cannot install lossless native result decoding."
        raise TypeError(message)

    def create_request(*args: Any, **options: Any) -> _ExactRequest:
        return _ExactRequest(request_factory(*args, **options))

    def cursor(*args: Any, **options: Any) -> Any:
        bound = inspect.signature(cursor_factory).bind_partial(*args, **options)
        raw_requested = bound.arguments.get(interface.raw_flag) is interface.raw_value
        if bound.arguments.get("cursor_style", "row") != "row":
            message = "Exact Trino results require the row cursor style."
            raise ValueError(message)
        bound.arguments[interface.raw_flag] = interface.raw_value
        native = cursor_factory(*bound.args, **bound.kwargs)
        return native if raw_requested else _ExactCursor(native)

    setattr(connection, _REQUEST_FACTORY, create_request)
    connection.cursor = cursor
    return connection
