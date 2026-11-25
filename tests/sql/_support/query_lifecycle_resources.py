"""Actual GP CREATE acknowledgements for native query lifecycle unit seams."""

from __future__ import annotations

from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.gp.creation_identity import execute_created_table
from analytics_toolkit.sql.ddl.creation_resources import CreatedTableResource, configuration_digest

from tests.sql._support.query_native_assignment import QueryNativeConnection, QueryNativeDatabase


def query_lifecycle_connection():
    database = QueryNativeDatabase([], native_type="CIDR")
    return QueryNativeConnection(database, "gp")


def acknowledge_query_resources(options, owned):
    connection = query_lifecycle_connection()
    receipts = {}
    for name, _logical in owned.state.owned_stage_resources:
        if name in receipts:
            continue
        candidate = CreatedTableResource(
            name,
            "gp",
            connection_key=options.to_db_key,
            configuration_digest=configuration_digest(options.to_db_key),
        )
        receipts[name] = execute_created_table(
            get_backend_adapter("gp"),
            connection,
            f"CREATE TABLE {name} (payload CIDR)",
            candidate,
        )
    owned.state.owned_stage_scoped_resources[:] = [
        (receipts[name], logical) for name, logical in owned.state.owned_stage_resources
    ]
    owned.state.scoped_stage_ownership_requested = True
