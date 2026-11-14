"""Adaptive transfer orchestration with a real SDK/raw-HTTP Trino source."""

from __future__ import annotations

from dataclasses import dataclass, field

from analytics_toolkit.sql.backends.models import SourceColumn
from analytics_toolkit.sql.connection.get_sql_connection import get_sql_connection
from analytics_toolkit.sql.dml.transfer.io.source import iter_source_batches

from tests.sql._support.adaptive_transfer import (
    Connection,
    Scenario,
    adaptive_input,
    attempt,
    install,
    normalized_source_rows,
)
from tests.sql._support.exact_trino_values import column, raw_pages_response


@dataclass
class RawTransferSource:
    native_type: str
    signature: dict
    queries: dict
    statements: list = field(default_factory=list)
    calls: list = field(default_factory=list)


def install_raw_trino_transfer(monkeypatch, write_sql_connections, source, **options):
    configuration = {
        "type": "trino",
        "host": "trino.example",
        "port": 8080,
        "user": "unit",
        "catalog": "iceberg",
        "schema": "sandbox",
    }
    write_sql_connections({"trino_source": configuration, "trino_target": configuration})

    def read(scenario, key, backend, connection_ref, query, *args, **kwargs):
        scenario.read_count += 1
        scenario.events.append(f"read:{query}")
        pages = [page for page in source.queries[query] if page is not None]
        statements = raw_pages_response(
            monkeypatch,
            [column("stamp", source.native_type, source.signature)],
            pages,
            calls=source.calls,
        )
        try:
            yield from iter_source_batches(key, backend, connection_ref, query, *args, **kwargs)
        finally:
            source.statements.extend(statements)
        scenario.events.append(f"complete:{query}")

    monkeypatch.setattr(Scenario, "read", read)
    scenario = install(
        monkeypatch,
        native_type=source.native_type,
        from_db_key="trino_source",
        from_db_backend="trino",
        to_db_key="trino_target",
        source_sql="SELECT stamp FROM source",
        **options,
    )
    scenario.source_schema = [SourceColumn("stamp", source.native_type)]

    def connect(alias):
        if alias == "trino_source":
            scenario.source_open_count += 1
            if scenario.source_unavailable:
                message = "The source cannot be reopened after its certified scan."
                raise ConnectionError(message)
            return get_sql_connection(alias)
        return Connection()

    monkeypatch.setattr(attempt, "get_sql_connection", connect)

    def load(options, connection_refs, stage_state, *_args, **_kwargs):
        refs, state = connection_refs, stage_state
        scenario.schemas.append(dict(state.stage_column_types or {}))
        values = []
        scenario.loaded.append(values)
        for batch in adaptive_input.resolved_source_batches(
            options,
            scenario.read,
            options.from_db_key,
            options.from_db_backend,
            refs.source,
            options.source_sql,
            options.batch_size,
            retry_cnt=_kwargs.get("read_retry_cnt", options.retry_cnt),
            timeout_increment=options.timeout_increment,
        ):
            rows = normalized_source_rows(options, refs, state, batch)
            values.extend(rows)
            if rows:
                scenario.events.append("write_batch")
        scenario.events.append("load")
        return len(values)

    monkeypatch.setattr(attempt, "load_stage_batches", load)
    return scenario
