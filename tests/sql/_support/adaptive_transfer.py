from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.general import set_connections_path
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.models import SourceColumn
from analytics_toolkit.sql.dml.transfer.flow import (
    adaptive_input,
    api,
    attempt,
    finalize,
    row_counts,
    stage,
    staged_attempt,
    staged_keyed_pipeline,
)
from analytics_toolkit.sql.dml.transfer.io.source import normalize_source_batch
from analytics_toolkit.sql.dml.transfer.runtime import retry as transfer_retry
from analytics_toolkit.sql.dml.transfer.runtime.models import (
    RowBatch,
    TransferOptions,
    TransferSlice,
    TransferStageState,
)
from analytics_toolkit.sql.dml.transfer.runtime.retry import run_with_fresh_connection
from analytics_toolkit.sql.execution.backend_options import (
    backend_operation,
    current_backend_operation,
)
from analytics_toolkit.sql.execution.cancellation import AsyncSqlCancelled

_REAL_LOAD_STAGE_BATCHES = attempt.load_stage_batches
# Keep real target dispatch while replacing only network and database boundaries.
_REAL_TARGET_RUNNER = attempt._run_with_fresh_target_connection


@dataclass
class Scenario:
    options: TransferOptions
    source_schema: list[SourceColumn]
    batches: dict[str, list[RowBatch]]
    events: list[str] = field(default_factory=list)
    loaded: list[list[tuple[Any, ...]]] = field(default_factory=list)
    schemas: list[dict[str, str]] = field(default_factory=list)
    finalizations: int = 0
    read_count: int = 0
    fail_finalization_once: bool = False
    make_source_unavailable_on_retry: bool = False
    source_unavailable: bool = False
    source_open_count: int = 0
    fail_stream: bool = False
    cancel_stream: bool = False
    existing_types: dict[str, str] | None = None
    change_scope: bool = False
    change_configuration: bool = False
    change_configuration_on_retry: bool = False
    change_configuration_after_insert: bool = False
    reconnect_after_configuration_change: bool = False
    configuration_field: str = "database"
    configuration_value: str = "changed_namespace"
    unexpected_driver_opens: list[str] = field(default_factory=list)

    def run(self, *, metadata: bool = False, dry_run: bool = False) -> Any:
        return sql.transfer(
            self.options.from_db_key,
            self.options.to_db_key,
            from_sql=self.options.source_sql,
            to_table=self.options.target_table,
            return_metadata=metadata,
            dry_run=dry_run,
        )

    def read(
        self, _key: str, _backend: str, _ref: Any, query: str, *_args: Any, **_kwargs: Any
    ) -> Any:
        self.read_count += 1
        self.events.append(f"read:{query}")
        for batch_index, batch in enumerate(self.batches[query]):
            if self.change_scope:
                operation = current_backend_operation()
                assert operation is not None
                operation.statuses[("ch", "audit")] = {
                    "alias": "ch",
                    "cluster": "audit",
                    "complete": True,
                    "table": "source",
                    "covered_hosts": [f"h{batch_index}"],
                }
            yield batch
        if self.fail_stream:
            message = "stream interrupted"
            raise ConnectionError(message)
        if self.cancel_stream:
            raise AsyncSqlCancelled
        if self.change_configuration:
            _change_configuration(self.configuration_field, self.configuration_value)
        self.events.append(f"complete:{query}")


class Connection:
    server_tz = "UTC"
    effective_read_timezone = "UTC"

    def close(self) -> None:
        return


def _change_configuration(field: str = "database", value: str = "changed_namespace") -> None:
    # The autouse fixture owns this temporary project; no host config is used.
    path = Path(".connections")
    configs = json.loads(path.read_text())
    configs["ch"][field] = value
    path.write_text(json.dumps(configs))
    set_connections_path(path)


def normalized_source_rows(options, refs, state, batch):
    if options.input_resolution.get("spool") is not None:
        return batch.rows
    return normalize_source_batch(
        options, refs.source.get("connection"), batch, state.source_column_types
    ).rows


def install(
    monkeypatch: pytest.MonkeyPatch,
    *,
    native_type: str = "Int256",
    rows: list[tuple[Any, ...]] | None = None,
    **overrides: Any,
) -> Scenario:
    options = TransferOptions(
        from_db_key="ch",
        from_db_backend="ch",
        to_db_key="trino",
        to_db_backend="trino",
        source_sql="SELECT id FROM source",
        target_table="sandbox.target",
        canonical_destination_identity="sandbox.target",
        batch_size=1,
        min_batch_size=1,
        max_batch_size=1,
        adaptive_batch_size=False,
        retry_cnt=1,
        full_retry_cnt=2,
        timeout_increment=0,
        full_timeout_increment=0,
        trino_mode="values",
        validate_row_count=False,
    )
    options = replace(options, **overrides)
    scenario = Scenario(
        options,
        [SourceColumn("id", native_type)],
        {options.source_sql: [RowBatch(["id"], [row]) for row in (rows or [(0,), (1,)])]},
    )
    monkeypatch.setattr(api, "build_transfer_options", lambda **_kwargs: scenario.options)

    def connect(alias: str) -> Connection:
        if alias == scenario.options.from_db_key:
            scenario.source_open_count += 1
            if scenario.source_unavailable:
                message = "The original source is unavailable after certification"
                raise ConnectionError(message)
        return Connection()

    monkeypatch.setattr(attempt, "get_sql_connection", connect)
    monkeypatch.setattr(
        attempt,
        "create_stage_state",
        lambda *_args: TransferStageState(
            target_exists=scenario.existing_types is not None,
        ),
    )
    monkeypatch.setattr(
        attempt, "inspect_source_query_schema", lambda *_args: scenario.source_schema
    )
    monkeypatch.setattr(
        attempt,
        "_run_with_fresh_target_connection",
        lambda _options, _role, operation: operation({"connection": Connection()}),
    )
    monkeypatch.setattr(
        adaptive_input,
        "run_with_fresh_connection",
        lambda _key, _role, operation, **_kwargs: operation({"connection": Connection()}),
    )
    monkeypatch.setattr(
        adaptive_input,
        "get_existing_target_insert_types",
        lambda *_args, **_kwargs: scenario.existing_types,
    )

    read = scenario.read

    def load(
        options: TransferOptions,
        connection_refs: Any,
        stage_state: TransferStageState,
        *_args: Any,
        **_kwargs: Any,
    ) -> int:
        refs, state = connection_refs, stage_state
        scenario.schemas.append(dict(state.stage_column_types or {}))

        values = [
            row
            for batch in adaptive_input.resolved_source_batches(
                options,
                read,
                options.from_db_key,
                options.from_db_backend,
                refs.source,
                options.source_sql,
                options.batch_size,
            )
            for row in normalized_source_rows(options, refs, state, batch)
        ]
        scenario.loaded.append(values)
        scenario.events.append("load")
        return len(values)

    def ensure(*_args: Any, **_kwargs: Any) -> None:
        scenario.events.append("create_target")

    def finish(*_args: Any, **_kwargs: Any) -> None:
        scenario.finalizations += 1
        if scenario.fail_finalization_once and scenario.finalizations == 1:
            scenario.batches[options.source_sql] = [RowBatch(["id"], [(999,)])]
            if scenario.make_source_unavailable_on_retry:
                scenario.source_unavailable = True
            if scenario.change_configuration_on_retry:
                _change_configuration(scenario.configuration_field, scenario.configuration_value)
            message = "lost target acknowledgement"
            raise ConnectionError(message)
        scenario.events.append("publish")

    monkeypatch.setattr(attempt, "iter_source_batches", read)
    monkeypatch.setattr(adaptive_input, "iter_source_batches", read)
    monkeypatch.setattr(attempt, "load_stage_batches", load)
    monkeypatch.setattr(stage, "ensure_transfer_target_table", ensure)
    monkeypatch.setattr(attempt, "ensure_transfer_target_table", ensure)
    monkeypatch.setattr(finalize, "finalize_loaded_stage", finish)
    monkeypatch.setattr(attempt, "finalize_loaded_stage", finish)
    monkeypatch.setattr(row_counts, "validate_loaded_stage_row_count", lambda **_kwargs: None)
    monkeypatch.setattr(attempt, "validate_loaded_stage_row_count", lambda **_kwargs: None)
    monkeypatch.setattr(attempt, "cleanup_attempt_stages", lambda *_args: None)
    monkeypatch.setattr(attempt, "cleanup_stage", lambda **_kwargs: None)
    monkeypatch.setattr(api, "best_effort_transfer_target_count", lambda *_args, **_kwargs: None)
    return scenario


def track_spools(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    paths = []
    original = adaptive_input.ReplaySpool

    def spool(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        paths.append(result.path)
        return result

    monkeypatch.setattr(adaptive_input, "ReplaySpool", spool)
    return paths


def require_streaming_fast_path(monkeypatch: pytest.MonkeyPatch) -> None:
    def spool(*_args: Any, **_kwargs: Any) -> Any:
        message = "Metadata-proven transfer must not profile or spool"
        raise AssertionError(message)

    monkeypatch.setattr(adaptive_input, "ReplaySpool", spool)


def install_native_snapshot(monkeypatch: pytest.MonkeyPatch, scenario: Scenario) -> list[str]:
    events = []
    snapshot = "source_stage.native_checkpoint"
    query = "SELECT `id` FROM source_stage.native_checkpoint"
    scenario.batches[query] = scenario.batches[scenario.options.source_sql]
    monkeypatch.setattr(staged_attempt, "get_sql_connection", lambda _key: Connection())
    monkeypatch.setattr(
        staged_attempt, "create_stage_state", lambda *_args: TransferStageState(False)
    )
    monkeypatch.setattr(
        staged_attempt, "inspect_source_query_schema", lambda *_args: scenario.source_schema
    )
    monkeypatch.setattr(staged_attempt, "cleanup_stage", lambda *_args, **_kwargs: None)

    def materialize(*_args: Any) -> Any:
        events.append("snapshot")
        counts = (
            {item.index: 1 for item in scenario.options.transfer_slices}
            if scenario.options.transfer_slices
            else {0: sum(batch.row_count for batch in scenario.batches[query])}
        )
        return snapshot, counts

    def drop(*_args: Any, **_kwargs: Any) -> None:
        spool = scenario.options.input_resolution["spool"]
        assert spool.certified
        assert spool.path.is_file()
        events.append("drop_after_certification")

    monkeypatch.setattr(staged_attempt, "_materialize_snapshot", materialize)
    monkeypatch.setattr(adaptive_input, "cleanup_stage_table", drop)
    return events


@dataclass
class ConnectionCounts:
    active: dict[str, int] = field(default_factory=dict)
    maximum: dict[str, int] = field(default_factory=dict)

    def open(self, alias: str) -> CountedConnection:
        self.active[alias] = self.active.get(alias, 0) + 1
        self.maximum[alias] = max(self.maximum.get(alias, 0), self.active[alias])
        return CountedConnection(self, alias)


class CountedConnection:
    server_tz = "UTC"
    effective_read_timezone = "UTC"

    def __init__(self, counts: ConnectionCounts, alias: str) -> None:
        self.counts = counts
        self.alias = alias
        self.closed = False

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.counts.active[self.alias] -= 1


def install_capped_keyed_replay(
    monkeypatch: pytest.MonkeyPatch,
    scenario: Scenario,
) -> ConnectionCounts:
    scenario.options = replace(
        scenario.options,
        source_transfer_staging_schema="source_stage",
        transfer_slices=[
            TransferSlice(0, (1,), "", "SELECT first", "key=1"),
            TransferSlice(1, (2,), "", "SELECT last", "key=2"),
        ],
        progress=True,
        estimate_total_rows=True,
    )
    install_native_snapshot(monkeypatch, scenario)
    counts = ConnectionCounts()
    monkeypatch.setattr(staged_keyed_pipeline, "get_sql_connection", counts.open)
    monkeypatch.setattr(attempt, "get_sql_connection", counts.open)
    monkeypatch.setattr(adaptive_input, "run_with_fresh_connection", run_with_fresh_connection)
    monkeypatch.setattr(attempt, "_run_with_fresh_target_connection", _REAL_TARGET_RUNNER)
    monkeypatch.setattr(attempt, "load_stage_batches", _REAL_LOAD_STAGE_BATCHES)
    monkeypatch.setattr(
        staged_keyed_pipeline, "create_stage_state", lambda *_args: TransferStageState(False)
    )
    monkeypatch.setattr(
        staged_keyed_pipeline, "inspect_source_query_schema", lambda *_args: scenario.source_schema
    )
    monkeypatch.setattr(staged_keyed_pipeline, "cleanup_stage", lambda *_args, **_kwargs: None)

    def initialize(**kwargs: Any) -> None:
        state = kwargs["stage_state"]
        state.stage_table = "stage.payload"
        state.stage_table_created = True
        state.first_non_empty_batch = pd.DataFrame(columns=state.source_columns)

    def insert(*args: Any, **kwargs: Any) -> int:
        rows = list(args[4])
        scenario.loaded.append(rows)
        scenario.schemas.append(dict(kwargs["target_column_types"]))
        kwargs["on_success"](1.0, len(rows))
        kwargs["on_progress"](len(rows))
        if scenario.change_configuration_after_insert and len(scenario.loaded) == 1:
            _change_configuration(scenario.configuration_field, scenario.configuration_value)
            if scenario.reconnect_after_configuration_change:
                transfer_retry.replace_connection("trino", args[1])
        return len(rows)

    def validate(**kwargs: Any) -> None:
        kwargs["target_connection_runner"]("validate", lambda _ref: None)

    def publish(*_args: Any, **kwargs: Any) -> None:
        kwargs["target_connection_runner"]("finalize", lambda _ref: None)
        scenario.finalizations += 1

    def estimate(*_args: Any, **_kwargs: Any) -> None:
        message = "Certified replay must use its input count without a live source estimate"
        raise AssertionError(message)

    monkeypatch.setattr(attempt, "initialize_stage_for_first_batch", initialize)
    monkeypatch.setattr(attempt, "insert_rows_batch", insert)
    monkeypatch.setattr(attempt, "estimate_source_rows", estimate)
    monkeypatch.setattr(row_counts, "validate_loaded_stage_row_count", validate)
    monkeypatch.setattr(finalize, "finalize_loaded_stage", publish)
    # A reconnect must be rejected before even a fake driver opens against the
    # newly selected configuration. All actual driver boundaries remain fake.
    adapter = get_backend_adapter("trino")

    def unexpected_open(config: Any, **_kwargs: Any) -> CountedConnection:
        scenario.unexpected_driver_opens.append(config.connection_key)
        return counts.open(config.connection_key)

    monkeypatch.setattr(adapter, "open_connection", unexpected_open)
    return counts


__all__ = [
    "AsyncSqlCancelled",
    "RowBatch",
    "Scenario",
    "SourceColumn",
    "TransferSlice",
    "adaptive_input",
    "backend_operation",
    "install",
    "pytest",
]


def fake_target_schema(
    monkeypatch: pytest.MonkeyPatch, backend: str, types: dict[str, str]
) -> None:
    """Supply the authoritative metadata now required before any stage DDL."""
    adapter = get_backend_adapter(backend)
    monkeypatch.setattr(adapter, "get_table_column_types", lambda *_args, **_kwargs: dict(types))
    monkeypatch.setattr(
        adapter, "get_writable_table_column_types", lambda *_args, **_kwargs: dict(types)
    )
