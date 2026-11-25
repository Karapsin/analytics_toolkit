"""Public staged transfers retain exact source policy and bounded range retries."""

from __future__ import annotations

import datetime as dt
import struct
from io import BytesIO
from types import SimpleNamespace
from typing import Any

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends.ch.http_codec import ExactHTTPClient
from analytics_toolkit.sql.backends.ch.native_client import NativeClickHouseClient
from analytics_toolkit.sql.backends.ch.native_codec import exact_block_stream
from analytics_toolkit.sql.connection.errors import InvalidSqlInputError
from analytics_toolkit.sql.core.exact_values import ExactTemporal
from analytics_toolkit.sql.dml.transfer.flow import api, staged_attempt, staged_keyed_io
from analytics_toolkit.sql.dml.transfer.flow.lazy_keyed_runtime import freeze_attempt_metadata
from analytics_toolkit.sql.dml.transfer.flow.range_scheduler import (
    AdaptiveRangeScheduler,
    OrdinalRange,
)
from analytics_toolkit.sql.dml.transfer.flow.stage_identity import resolve_internal_columns
from analytics_toolkit.sql.dml.transfer.runtime.models import RowBatch, TransferStageState
from zoneinfo import ZoneInfo

from tests.sql._support import gp_source_codecs as gp_source_support
from tests.sql._support.gp_float_wire import FloatCursor, float_source
from tests.sql._support.staged_keyed import _concurrency, _ready_task
from tests.sql._support.transfer_ordinal import _staged_options
from tests.sql.backends.ch._support.exact_transport import (
    BinaryNativeSource,
    http_query_context,
    http_source,
    native_context,
)

_TYPES = {"stamp": "DateTime", "precise": "DateTime64(9)"}


def _binary() -> bytes:
    from clickhouse_driver.writer import write_binary_str  # noqa: PLC0415

    buffer = BytesIO(b"\x02\x01")
    buffer.seek(2)
    for name, native, payload in (
        ("stamp", "DateTime", struct.pack("<I", 0)),
        ("precise", "DateTime64(9)", struct.pack("<q", 123456789)),
    ):
        write_binary_str(name, buffer)
        write_binary_str(native, buffer)
        buffer.write(payload)
    return buffer.getvalue()


def _binary_client(transport: str) -> Any:
    if transport == "native":
        from clickhouse_driver import Client  # noqa: PLC0415

        raw = Client("127.0.0.1")
        raw.connection.context.server_info = native_context().server_info
        raw.connection.context.server_info.get_timezone = lambda: "Europe/Moscow"
        client = NativeClickHouseClient(raw)

        def execute(_query: str, **kwargs: Any) -> Any:
            block = exact_block_stream(BinaryNativeSource(_binary()), raw.connection.context).read()
            return (
                block.get_columns() if kwargs.get("columnar") else block.get_rows(),
                block.columns_with_types,
            )

        raw.execute = execute
        return client
    from clickhouse_connect.driver.transform import NativeTransform  # noqa: PLC0415

    class Raw:
        server_tz = ZoneInfo("Europe/Moscow")
        _transform = NativeTransform()

        def query(self, _query: str, **kwargs: Any) -> Any:
            context = http_query_context(column_oriented=kwargs.get("column_oriented", False))
            return self._transform.parse_response(http_source(_binary()), context)

    return ExactHTTPClient(Raw())


def _public_attempt(monkeypatch: Any, options: Any, operation: Any) -> int:
    # Keep public retry/metadata orchestration; replace only acquisition of the
    # already certified snapshot with this component's real staged read/worker.
    monkeypatch.setattr(api, "build_transfer_options", lambda **_kwargs: options)
    monkeypatch.setattr(api, "run_transfer_attempt", lambda options, **_kwargs: operation(options))
    return sql.transfer("source", "target")


def _read(options: Any, connection: Any, keyed: bool, types: dict[str, str]) -> RowBatch:
    internal = resolve_internal_columns(list(types), options.from_db_backend)
    if keyed:
        metadata = freeze_attempt_metadata(
            source_columns=list(types),
            source_column_types=types,
            stage_column_types=types,
            internal_columns=internal,
        )
        task = _ready_task((options.transfer_slices or [])[0], "source.snapshot", 1)
        return staged_keyed_io.read_key_batch(
            options, {"connection": connection}, task, metadata, 1, 2
        )
    return staged_attempt._read_snapshot_range(
        options,
        connection,
        "source.snapshot",
        list(types),
        TransferStageState(
            target_exists=True, internal_columns=internal, source_column_types=types
        ),
        OrdinalRange(0, 1, 2),
    )


@pytest.mark.parametrize("transport", ["http", "native"])
@pytest.mark.parametrize("keyed", [False, True])
def test_public_staged_eager_reads_enable_real_binary_exact_temporals(
    monkeypatch: Any, transport: str, keyed: bool
) -> None:
    from analytics_toolkit.sql.dml.transfer.runtime.models import TransferSlice  # noqa: PLC0415

    connection = _binary_client(transport)
    options = _staged_options(
        from_db_backend="ch",
        replace_target_table=False,
        transfer_concurrency=_concurrency(1, 1),
        transfer_slices=[TransferSlice(0, (1,), "", "SELECT 1", "key=1")] if keyed else None,
        retry_cnt=1,
    )
    captured = []

    def attempt(current: Any) -> int:
        batch = _read(current, connection, keyed, _TYPES)
        captured.extend(batch.rows)
        return batch.row_count

    assert _public_attempt(monkeypatch, options, attempt) == 1
    assert captured == [(ExactTemporal(0, 0, "UTC"), ExactTemporal(123456789, 9, "UTC"))]
    assert options.input_resolution.get("source_timezone") == "UTC"


@pytest.mark.parametrize("keyed", [False, True])
def test_public_staged_eager_reads_bind_actual_moscow_source_policy(
    monkeypatch: Any, keyed: bool
) -> None:
    from analytics_toolkit.sql.dml.transfer.runtime.models import TransferSlice  # noqa: PLC0415

    options = _staged_options(
        from_db_backend="ch",
        replace_target_table=False,
        transfer_concurrency=_concurrency(1, 1),
        transfer_slices=[TransferSlice(0, (1,), "", "SELECT 1", "key=1")] if keyed else None,
        retry_cnt=1,
    )
    connection = SimpleNamespace(effective_read_timezone="Europe/Moscow")
    reader = staged_keyed_io if keyed else staged_attempt
    monkeypatch.setattr(
        reader,
        "_read_backend",
        lambda *_args, **_kwargs: SimpleNamespace(
            column_names=["stamp"],
            columns=[[dt.datetime(2024, 1, 1, 12)]],  # noqa: DTZ001 - source wall-clock needs policy binding.
        ),
    )
    captured = []

    def attempt(current: Any) -> int:
        batch = _read(current, connection, keyed, {"stamp": "DateTime"})
        captured.extend(batch.rows)
        return batch.row_count

    assert _public_attempt(monkeypatch, options, attempt) == 1
    assert captured[0][0].tzinfo is not None
    assert captured[0][0].astimezone(dt.timezone.utc) == dt.datetime(
        2024, 1, 1, 9, tzinfo=dt.timezone.utc
    )
    assert options.input_resolution.get("source_timezone") == "Europe/Moscow"


@pytest.mark.parametrize("keyed", [False, True])
def test_public_staged_gp_eager_reads_use_exact_float_wire_once(
    monkeypatch: Any, keyed: bool
) -> None:
    from analytics_toolkit.sql.dml.transfer.runtime.models import TransferSlice  # noqa: PLC0415

    _scenario, connection, exact = float_source(monkeypatch, 701)

    class EagerCursor(FloatCursor):
        def execute(self, query: str) -> None:
            self.connection.queries.append(query)
            self.probe = "WHERE 1 = 0" in query
            self.binary = "pg_catalog.float" in query
            if self.binary:
                self.description = [("_atk_0", 17, None, None, None, None, None)]
            if not self.probe:
                assert "source.snapshot" in query
                assert not query.lstrip().upper().startswith("SET")
                self.connection.executed_inputs.append(query)

        def fetchall(self) -> Any:
            if not self.binary and self.connection.oid not in self.casters:
                return [(float(self.connection.documents[0]),)]
            return self.fetchmany(1)

    monkeypatch.setattr(gp_source_support, "SourceCursor", EagerCursor)
    options = _staged_options(
        replace_target_table=False,
        retry_cnt=1,
        transfer_concurrency=_concurrency(1, 1),
        transfer_slices=[TransferSlice(0, (1,), "", "SELECT 1", "key=1")] if keyed else None,
    )
    captured = []

    def attempt(current: Any) -> int:
        batch = _read(current, connection, keyed, {"id": "double precision"})
        captured.extend(batch.rows)
        return batch.row_count

    assert _public_attempt(monkeypatch, options, attempt) == 1
    assert len(connection.executed_inputs) == 1
    assert struct.pack("!d", captured[0][0]) == struct.pack("!d", exact)
    assert all(cursor.closed for cursor in connection.cursors)


@pytest.mark.parametrize("keyed", [False, True])
def test_public_staged_read_deterministic_errors_never_reconnect(
    monkeypatch: Any, keyed: bool
) -> None:
    from analytics_toolkit.sql.dml.transfer.runtime.models import TransferSlice  # noqa: PLC0415

    options = _staged_options(
        replace_target_table=False,
        retry_cnt=3,
        timeout_increment=0,
        transfer_concurrency=_concurrency(1, 1),
        transfer_slices=[TransferSlice(0, (1,), "", "SELECT 1", "key=1")] if keyed else None,
    )
    opened = []
    replacements = []
    reads = []
    connection = SimpleNamespace(close=lambda: None)
    monkeypatch.setattr(
        staged_attempt, "get_sql_connection", lambda key: opened.append(key) or connection
    )
    reader = staged_keyed_io if keyed else staged_attempt

    def read(*_args: Any, **_kwargs: Any) -> Any:
        reads.append(1)
        message = "Unknown source column"
        raise InvalidSqlInputError(message)

    monkeypatch.setattr(reader, "_read_backend", read)
    monkeypatch.setattr(staged_attempt, "replace_connection", lambda *_args: replacements.append(1))
    monkeypatch.setattr(
        staged_keyed_io, "_replace_managed_connection", lambda *_args: replacements.append(1)
    )
    state = TransferStageState(
        target_exists=True,
        internal_columns=resolve_internal_columns(["id"], "gp"),
        source_column_types={"id": "bigint"},
    )

    def attempt(current: Any) -> int:
        if keyed:
            _read(current, connection, True, {"id": "bigint"})
        else:
            staged_attempt._range_worker(
                current,
                "snapshot",
                ["id"],
                state,
                "target.stage",
                AdaptiveRangeScheduler({0: 4}),
                0,
                1,
            )
        return 0

    with pytest.raises(InvalidSqlInputError, match="Unknown source column"):
        _public_attempt(monkeypatch, options, attempt)
    assert reads == [1]
    assert replacements == []


@pytest.mark.parametrize("reassign", [False, True])
def test_public_staged_range_retry_budget_survives_splits_and_worker_reassignment(
    monkeypatch: Any, reassign: bool
) -> None:
    options = _staged_options(
        replace_target_table=False,
        retry_cnt=2 if reassign else 3,
        timeout_increment=0,
        batch_size=4,
        min_batch_size=1,
        transfer_concurrency=_concurrency(1, 1),
    )
    connection = SimpleNamespace(close=lambda: None)
    monkeypatch.setattr(staged_attempt, "get_sql_connection", lambda _key: connection)
    monkeypatch.setattr(staged_attempt, "replace_connection", lambda *_args: None)
    reads = []

    def read(*args: Any, **_kwargs: Any) -> Any:
        reads.append(args[2])
        message = "temporary source transport failure"
        raise ConnectionError(message)

    monkeypatch.setattr(staged_attempt, "_read_backend", read)
    state = TransferStageState(
        target_exists=True,
        internal_columns=resolve_internal_columns(["id"], "gp"),
        source_column_types={"id": "bigint"},
    )
    scheduler = AdaptiveRangeScheduler({0: 4})
    original_claim = scheduler.claim
    first_claim = True

    def claim(worker: int, size: int) -> Any:
        nonlocal first_claim
        if reassign and worker == 0 and not first_claim:
            return None
        first_claim = False
        return original_claim(worker, size)

    monkeypatch.setattr(scheduler, "claim", claim)

    def attempt(current: Any) -> int:
        for worker in range(2 if reassign else 1):
            staged_attempt._range_worker(
                current, "snapshot", ["id"], state, "target.stage", scheduler, worker, 1
            )
        return 0

    with pytest.raises(ConnectionError, match="temporary source transport failure"):
        _public_attempt(monkeypatch, options, attempt)
    assert len(reads) == options.retry_cnt
