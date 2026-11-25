"""Fixture metadata must track creation identity through real lifecycle SQL."""

from __future__ import annotations

import pytest

from tests.sql._support.reconfigure import ReconfigureClient


def _uuid(client, name):
    rows = client.query(
        f"SELECT uuid FROM system.tables WHERE database = 'analytics' AND name = '{name}'"
    ).result_rows
    assert len(rows) == 1
    return rows[0][0]


@pytest.mark.parametrize("operation", ["rename", "exchange"])
def test_metadata_fixture_renames_preserve_actual_created_object_identity(operation):
    client = ReconfigureClient()
    client.command("CREATE TABLE analytics.identity_a (id UInt64) ENGINE=Memory")
    client.command("CREATE TABLE analytics.identity_b (id UInt64) ENGINE=Memory")
    first, second = _uuid(client, "identity_a"), _uuid(client, "identity_b")
    assert first != second

    if operation == "rename":
        client.command("RENAME TABLE analytics.identity_a TO analytics.identity_renamed")
        assert _uuid(client, "identity_renamed") == first
        assert _uuid(client, "identity_b") == second
    else:
        client.command("EXCHANGE TABLES analytics.identity_a AND analytics.identity_b")
        assert _uuid(client, "identity_a") == second
        assert _uuid(client, "identity_b") == first


def test_metadata_fixture_drop_recreate_allocates_a_fresh_creation_identity():
    client = ReconfigureClient()
    statement = "CREATE TABLE analytics.identity_a (id UInt64) ENGINE=Memory"
    client.command(statement)
    first = _uuid(client, "identity_a")

    client.command("DROP TABLE analytics.identity_a")
    client.command(statement)

    assert _uuid(client, "identity_a") != first


def test_metadata_fixture_honors_an_explicit_create_generation_uuid():
    client = ReconfigureClient()
    identity = "12345678-1234-5678-9abc-123456789abc"

    client.command(f"CREATE TABLE analytics.identity_a UUID '{identity}' (id UInt64) ENGINE=Memory")

    assert _uuid(client, "identity_a") == identity
