"""Public Parquet transfer with real stage SQL and an in-memory external table boundary."""

from __future__ import annotations

from copy import deepcopy

from analytics_toolkit import sql
from analytics_toolkit.sql.dml.transfer.flow import (
    attempt,
    finalize,
    keyed_pipeline,
    parquet_batches,
)
from sqlglot import exp, parse_one

from tests.conftest import DEFAULT_SQL_CONNECTIONS
from tests.sql._support.transfer_not_null import install_transfer_database


def _command_handler(database):
    execute = database.execute

    def command(connection, statement, params=None):
        tree = parse_one(statement, read="trino")
        table = tree.find(exp.Table) if tree is not None else None
        incoming = isinstance(tree, exp.Create) and table is not None and table.catalog == "hive"
        if (
            isinstance(tree, exp.Drop)
            and table is not None
            and table.catalog == "hive"
            and database.hive_drop_failure is not None
        ):
            raise database.hive_drop_failure
        if incoming:
            state = database.stage_states[-1]
            database.locations[database.identity(table)] = state.stage_external_location
            database.before_create.append(
                (
                    state.stage_table,
                    state.stage_external_location,
                    list(state.stage_table_candidates),
                    list(state.owned_stage_resources),
                )
            )
            if database.race_creates:
                database.race_creates -= 1
                execute(connection, statement, params)
                identity = database.identity(table)
                foreign = [
                    {"id": index, "part": 303001, "payload": 1000 + index} for index in range(100)
                ]
                database.tables[identity].rows = deepcopy(foreign)
                database.files[state.stage_external_location] = deepcopy(foreign)
                database.foreign_collisions.append(
                    (table.sql(dialect="trino"), identity, foreign, state.stage_external_location)
                )
                message = "A different creator won the Hive table-name race"
                error = RuntimeError(message)
                error.error_name = "TABLE_ALREADY_EXISTS"
                raise error
        result = execute(connection, statement, params)
        if incoming:
            database.incoming.append((table.sql(dialect="trino"), database.identity(table)))
            if database.lost_creates:
                database.lost_creates -= 1
                message = "Hive CREATE applied but its acknowledgement was lost"
                raise ConnectionError(message)
        return result

    return command


def install(monkeypatch, write_sql_connections, *, lost_creates=0, race_creates=0):
    connections = deepcopy(DEFAULT_SQL_CONNECTIONS)
    connections["trino"].update(
        s3_transfer_staging_schema="hive.sandbox",
        s3_transfer_staging_location="s3://run-owned/transfer",
        transfer_staging_schema="iceberg.sandbox",
        upsert_partition_drop_sql_template=(
            "DELETE FROM {table} WHERE {partition_column} = {partition_value}"
        ),
    )
    write_sql_connections(connections)
    database = install_transfer_database(
        monkeypatch,
        "trino",
        columns=("id", "part", "payload"),
        not_null=(),
        rows=[(1, 202401, 100), (2, 202401, 200)],
        source_types={"id": "Int64", "part": "Int64", "payload": "Int64"},
    )
    database.target.rows = [
        {"id": 1, "part": 202401, "payload": 10},
        {"id": 9, "part": 202401, "payload": 90},
        {"id": 1, "part": 202402, "payload": 20},
    ]
    database.incoming = []
    database.before_create = []
    database.stage_states = []
    database.remote_removed = []
    database.files = {}
    database.locations = {}
    database.hive_drop_failure = None
    database.lost_creates = lost_creates
    database.race_creates = race_creates
    database.foreign_collisions = []
    matches = database.matches

    def match(condition, row):
        if isinstance(condition, exp.In):
            query = condition.args.get("query")
            values = (
                [result[0] for result in database.select(query, row)[0]]
                if query is not None
                else [database.value(item, row) for item in condition.expressions]
            )
            return database.value(condition.this, row) in values
        return matches(condition, row)

    create = attempt.create_parquet_stage_table

    def record_stage(*args, **kwargs):
        state = kwargs.get("stage_state") if kwargs else args[2]
        database.stage_states.append(state)
        return create(*args, **kwargs)

    def write(batch, **kwargs):
        assert kwargs["stage_external_location"].startswith("s3://run-owned/")
        _name, identity = database.incoming[-1]
        database.tables[identity].insert(batch.columns, batch.rows)
        database.files.setdefault(kwargs["stage_external_location"], []).extend(
            deepcopy(batch.rows)
        )
        return batch.row_count

    def remove(location, **kwargs):
        database.remote_removed.append(location)
        database.files.pop(location, None)

    monkeypatch.setattr(database, "execute", _command_handler(database))
    monkeypatch.setattr(database, "matches", match)
    monkeypatch.setattr(attempt, "create_parquet_stage_table", record_stage)
    monkeypatch.setattr(parquet_batches, "get_sql_connection", attempt.get_sql_connection)
    for module in (attempt, keyed_pipeline):
        monkeypatch.setattr(
            module, "ensure_parquet_staging_dependencies", lambda: (None, None, None)
        )
        monkeypatch.setattr(module, "write_batch_to_parquet_stage", write)
    monkeypatch.setattr(
        finalize,
        "cleanup_parquet_stage_location",
        remove,
    )
    return database


def transfer(*, keyed=False, attempts=1):
    return sql.transfer(
        "ch",
        "trino",
        from_sql="SELECT id, part, payload FROM source_rows" + (" WHERE {id}" if keyed else ""),
        to_table="sandbox.events",
        write_mode="upsert",
        key_columns="id",
        upsert_partition_column="part",
        trino_mode="parquet",
        ignore_source_staging=True,
        validate_row_count=False,
        retry_cnt=1,
        full_retry_cnt=attempts,
        timeout_increment=0,
        full_timeout_increment=0,
        batch_size=1,
        min_batch_size=1,
        adaptive_batch_size=False,
        **(
            {
                "transfer_keys": "id",
                "transfer_key_values": [1, 2],
                "read_concurrency": 1,
                "write_concurrency": 1,
            }
            if keyed
            else {}
        ),
    )
