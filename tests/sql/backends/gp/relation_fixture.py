"""Native fixture generations survive DML and permit guarded owned cleanup."""

from __future__ import annotations

import datetime as dt

from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.gp.creation_identity import (
    drop_created_resource,
    execute_created_table,
)
from analytics_toolkit.sql.ddl.creation_resources import CreatedTableResource, configuration_digest

from tests.sql._support.gp_dates import _DateConnection
from tests.sql._support.gp_timestamp_text import TimestampDatabase


def test_gp_timestamp_fixture_preserves_generation_through_insert_and_guarded_cleanup():
    database = TimestampDatabase("TIMESTAMP")
    connection = _DateConnection(database)
    adapter = get_backend_adapter("gp")
    resource = CreatedTableResource(
        "sandbox.owned_stage",
        "gp",
        connection_key="gp",
        configuration_digest=configuration_digest("gp"),
    )
    receipt = execute_created_table(
        adapter, connection, "CREATE TABLE sandbox.owned_stage (payload TIMESTAMP)", resource
    )
    adapter.insert_rows_batch(
        connection,
        resource.name,
        ["payload"],
        [(dt.datetime.fromisoformat("2025-01-02T00:00:00.000000"),)],
        target_column_types={"payload": "TIMESTAMP"},
        trino_insert_chunk_size=None,
        gp_insert_chunk_size=10,
        connection_type="gp",
        query_label=None,
        on_progress=None,
    )

    drop_created_resource(adapter, connection, receipt)

    assert set(database.tables) == {("sandbox", "events")}
