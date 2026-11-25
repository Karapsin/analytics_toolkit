"""Transfer endpoints using actual selected ClickHouse client encoders."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from types import SimpleNamespace

from analytics_toolkit.sql.backends import get_backend_adapter

from tests.conftest import DEFAULT_SQL_CONNECTIONS
from tests.sql._support.adaptive_transfer import (
    RowBatch,
    SourceColumn,
    adaptive_input,
    attempt,
    finalize,
    install,
)
from tests.sql.backends.ch._support.exact_transport import native_context
from tests.sql.backends.ch._support.network_transport import encode_normalized


@dataclass
class ClientTarget:
    schema: dict
    physical_schema: dict
    transport: str
    live_rows: list
    emitted_batches: list = field(default_factory=list)
    wire_columns: list = field(default_factory=list)
    validations: list = field(default_factory=list)
    metadata_queries: list = field(default_factory=list)


class ClientConnection:
    server_tz = "UTC"
    effective_read_timezone = "UTC"
    effective_write_timezone = "UTC"

    def __init__(self, target):
        self.target = target
        self.is_native_transport = target.transport == "native"
        self.settings = {"flatten_nested": 0}
        if self.is_native_transport:
            self.context = native_context()
            self.context.settings.update(self.settings)

    def close(self):
        return

    def query(self, statement):
        if statement == "SELECT getSetting('flatten_nested')":
            return SimpleNamespace(result_rows=[(0,)])
        assert statement == "DESCRIBE TABLE sandbox.private_payload"
        self.target.metadata_queries.append(statement)
        return SimpleNamespace(result_rows=list(self.target.physical_schema.items()))

    def insert(self, *, table, data, column_names, column_type_names):
        assert table == "sandbox.private_payload"
        assert column_names == list(self.target.schema)
        assert column_type_names == list(self.target.schema.values())
        rows = deepcopy(list(data))
        self.target.emitted_batches.append(rows)
        # Native INSERT consumes the server sample types after the adapter uses
        # column_type_names as normalization hints. HTTP row INSERT uses the
        # supplied raw type headers directly.
        wire_types = (
            list(self.target.physical_schema.values())
            if self.is_native_transport
            else column_type_names
        )
        for index, (name, native) in enumerate(zip(column_names, wire_types)):
            wire = encode_normalized(
                native,
                [row[index] for row in rows],
                self.target.transport,
                settings=self.settings,
            )
            self.target.wire_columns.append((name, native, wire))


def insert_client_rows(target, column_types, rows):
    get_backend_adapter("ch").insert_rows_batch(
        ClientConnection(target),
        "sandbox.private_payload",
        list(column_types),
        rows,
        target_column_types=column_types,
        trino_insert_chunk_size=None,
        gp_insert_chunk_size=None,
        connection_type="ch",
        query_label=None,
        on_progress=None,
    )


def install_client_transfer(  # noqa: PLR0913 -- select source, target and actual client independently.
    monkeypatch,
    write_sql_connections,
    *,
    source_types,
    target_types,
    rows,
    transport="native",
    seed=None,
    physical_schema=None,
    **options,
):
    connections = deepcopy(DEFAULT_SQL_CONNECTIONS)
    connections["ch_target"] = {**connections["ch"], "database": "destination"}
    write_sql_connections(connections)
    columns = list(source_types)
    query = "SELECT " + ", ".join(columns) + " FROM source"
    scenario = install(
        monkeypatch,
        native_type=next(iter(source_types.values())),
        rows=rows,
        to_db_key="ch_target",
        to_db_backend="ch",
        source_sql=query,
        table_schema=target_types,
        **options,
    )
    scenario.source_schema = [SourceColumn(name, native) for name, native in source_types.items()]
    scenario.batches[query] = [RowBatch(columns, [deepcopy(row)]) for row in rows]
    if seed is not None:
        scenario.existing_types = dict(target_types)
    target = ClientTarget(
        dict(target_types),
        dict(target_types if physical_schema is None else physical_schema),
        transport,
        deepcopy(seed or []),
    )
    source_connect = attempt.get_sql_connection

    def connect(alias):
        return (
            source_connect(alias)
            if alias == scenario.options.from_db_key
            else ClientConnection(target)
        )

    monkeypatch.setattr(attempt, "get_sql_connection", connect)
    monkeypatch.setattr(
        attempt,
        "_run_with_fresh_target_connection",
        lambda _options, _role, operation: operation({"connection": ClientConnection(target)}),
    )
    monkeypatch.setattr(
        adaptive_input,
        "run_with_fresh_connection",
        lambda key, _role, operation, **_kwargs: operation({"connection": connect(key)}),
    )
    load = attempt.load_stage_batches

    def load_with_real_encoder(options, connection_refs, stage_state, *args, **kwargs):
        count = load(options, connection_refs, stage_state, *args, **kwargs)
        if count == 0:
            # The real loader never invokes a client writer without a batch.
            return count
        cached = options.input_resolution
        plan = cached["client_write_plan"]
        certificate = cached.get("client_write_certificate")
        spool = cached.get("spool")
        target.validations.append(
            {
                "capability": plan["capability"] if certificate is None else certificate[1],
                "certificate": certificate,
                "spool_digest": None if spool is None else spool.digest,
            }
        )
        insert_client_rows(target, stage_state.stage_column_types, scenario.loaded[-1])
        return count

    monkeypatch.setattr(attempt, "load_stage_batches", load_with_real_encoder)
    finish = finalize.finalize_loaded_stage

    def publish(options, *args, **kwargs):
        finish(options, *args, **kwargs)
        values = deepcopy(scenario.loaded[-1])
        target.live_rows = target.live_rows + values if options.write_mode == "append" else values

    monkeypatch.setattr(finalize, "finalize_loaded_stage", publish)
    monkeypatch.setattr(attempt, "finalize_loaded_stage", publish)
    return scenario, target
