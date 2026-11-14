"""Lossless readers detect supported native SDK interfaces without global patches."""

from __future__ import annotations

import importlib
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from analytics_toolkit.sql.backends.trino import exact_reads

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture(autouse=True)
def _isolated_sdk_interface_cache() -> Iterator[None]:
    exact_reads._sdk_interface.cache_clear()
    try:
        yield
    finally:
        exact_reads._sdk_interface.cache_clear()


class _MinimumCursor:
    def __init__(self) -> None:
        self.executed: list[tuple[str, list[tuple[int, ...]]]] = []

    def executemany(self, operation: str, parameters: list[tuple[int, ...]]) -> None:
        # SDK 0.320 executemany has no return statement; later SDKs return self.
        self.executed.append((operation, parameters))


class _MinimumConnection:
    """The verified 0.320 constructor/cursor flags, without a transport."""

    def __init__(self, host: str, *, experimental_python_types: bool = False) -> None:
        self.host = host
        self.experimental_python_types = experimental_python_types
        self.native_cursor = _MinimumCursor()
        self.cursor_calls: list[bool | None] = []

    def _create_request(self) -> object:
        return object()

    def cursor(self, *, experimental_python_types: bool | None = None) -> object:
        self.cursor_calls.append(experimental_python_types)
        return self.native_cursor


def _install_sdk_imports(
    monkeypatch: pytest.MonkeyPatch,
    *,
    connection: type = _MinimumConnection,
    factory: object = None,
    mapper_error: ModuleNotFoundError | None = None,
    fallback: bool = False,
) -> list[str]:
    real_import = importlib.import_module
    native_exceptions = real_import("trino.exceptions")
    calls: list[str] = []

    def load(name: str) -> object:
        calls.append(name)
        if name == "trino.dbapi":
            return SimpleNamespace(Connection=connection)
        if name == "trino.mapper":
            if mapper_error is not None:
                raise mapper_error
            if fallback:
                message = "No module named 'trino.mapper'"
                raise ModuleNotFoundError(message, name="trino.mapper")
            return SimpleNamespace(RowMapperFactory=factory)
        if name == "trino.client":
            return SimpleNamespace(RowMapperFactory=factory)
        if name == "trino.exceptions":
            return native_exceptions
        return real_import(name)

    monkeypatch.setattr(exact_reads, "importlib", SimpleNamespace(import_module=load))
    return calls


def _native_mapper_factory() -> type:
    try:
        mapping = importlib.import_module("trino.mapper")
    except ModuleNotFoundError as error:
        if error.name != "trino.mapper":
            raise
        mapping = importlib.import_module("trino.client")
    return mapping.RowMapperFactory


def test_exact_sdk_interfaces_minimum_driver_fallback_is_cached_and_keeps_raw_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = _native_mapper_factory()
    calls = _install_sdk_imports(monkeypatch, factory=factory, fallback=True)

    interface = exact_reads._sdk_interface()
    assert exact_reads._sdk_interface() is interface
    assert interface.raw_flag == "experimental_python_types"
    assert interface.raw_value is False
    assert interface.supports_encoding is False
    assert interface.mapper_factory is factory
    assert calls == ["trino.dbapi", "trino.mapper", "trino.client", "trino.exceptions"]

    connection = exact_reads.connect_exact(_MinimumConnection, host="trino.example", encoding=None)
    assert connection.host == "trino.example"
    assert connection.experimental_python_types is False
    assert connection.cursor(experimental_python_types=False) is connection.native_cursor
    assert connection.cursor_calls == [False]


def test_exact_sdk_interfaces_missing_raw_flag_is_actionable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UnsupportedConnection:
        pass

    calls = _install_sdk_imports(monkeypatch, connection=UnsupportedConnection)

    with pytest.raises(RuntimeError, match="supported exact result-mapping interface"):
        exact_reads._sdk_interface()
    assert calls == ["trino.dbapi"]


def test_exact_sdk_interfaces_minimum_executemany_preserves_native_none_return(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_sdk_imports(monkeypatch, factory=_native_mapper_factory(), fallback=True)
    connection = exact_reads.connect_exact(_MinimumConnection, host="trino.example")
    cursor = connection.cursor()
    parameters = [(1,), (2,)]

    assert cursor.executemany("INSERT INTO target VALUES (?)", parameters) is None
    assert connection.native_cursor.executed == [("INSERT INTO target VALUES (?)", parameters)]


@pytest.mark.parametrize("factory", [None, SimpleNamespace(_create_value_mapper=None)])
def test_exact_sdk_interfaces_missing_native_mapper_is_actionable(
    monkeypatch: pytest.MonkeyPatch,
    factory: object,
) -> None:
    calls = _install_sdk_imports(monkeypatch, factory=factory)

    with pytest.raises(RuntimeError, match="supported native value mapper"):
        exact_reads._sdk_interface()
    assert calls == ["trino.dbapi", "trino.mapper"]


def test_exact_sdk_interfaces_transitive_import_failure_does_not_trigger_legacy_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = "Missing SDK mapper dependency"
    missing_dependency = ModuleNotFoundError(message, name="native_mapper_dependency")
    calls = _install_sdk_imports(monkeypatch, mapper_error=missing_dependency)

    with pytest.raises(ModuleNotFoundError) as error:
        exact_reads._sdk_interface()
    assert error.value is missing_dependency
    assert calls == ["trino.dbapi", "trino.mapper"]


@pytest.mark.parametrize("missing_hook", ["both", "cursor", "_create_request"])
def test_exact_sdk_interfaces_connection_without_native_hooks_is_rejected(
    missing_hook: str,
) -> None:
    connection: object = SimpleNamespace(cursor=object, _create_request=object)
    if missing_hook == "both":
        connection = object()
    else:
        setattr(connection, missing_hook, None)

    def connect(**_kwargs: object) -> object:
        return connection

    with pytest.raises(TypeError, match="cannot install lossless native result decoding"):
        exact_reads.connect_exact(connect, host="trino.example")


def test_exact_sdk_interfaces_non_row_cursor_rejected_before_native_cursor_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RowStyleConnection(_MinimumConnection):
        def cursor(
            self,
            *,
            experimental_python_types: bool | None = None,
            cursor_style: str = "row",
        ) -> object:
            assert cursor_style == "row"
            return super().cursor(experimental_python_types=experimental_python_types)

    _install_sdk_imports(monkeypatch, factory=_native_mapper_factory())
    connection = exact_reads.connect_exact(RowStyleConnection, host="trino.example")

    with pytest.raises(ValueError, match="require the row cursor style"):
        connection.cursor(cursor_style="columnar")
    assert connection.cursor_calls == []
