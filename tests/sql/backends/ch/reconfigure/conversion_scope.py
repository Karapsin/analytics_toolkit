"""Local-to-pair conversion owns only the coordinator's original source table."""

from __future__ import annotations

import pytest
from analytics_toolkit.sql.backends.ch.cutover import StructuralCutoverError
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from analytics_toolkit.sql.connection.errors import InvalidSqlInputError
from sqlglot import exp, parse_one
from sqlglot.tokens import TokenType

from tests.sql._support.reconfigure import reconfigure_api
from tests.sql._support.reconfigure_conversion_scope import (
    ConversionScopeClient,
    configure_conversion,
    convert,
)


def drop_commands(commands):
    drops = []
    for statement in commands:
        tokens = ClickHouseDialect().tokenize(statement)
        if tokens and tokens[0].token_type is TokenType.DROP:
            drop = parse_one(statement, read=ClickHouseDialect)
            assert isinstance(drop, exp.Drop)
            drops.append(drop)
    return drops


def source_backup_cleanup(commands):
    return [
        drop
        for drop in drop_commands(commands)
        if any("__backup_" in table.name for table in drop.find_all(exp.Table))
    ]


@pytest.mark.parametrize("facade_scope", [None, "coordinator_only"])
def test_conversion_scope_preserves_distinct_remote_original_through_successful_public_cutover(
    monkeypatch, write_sql_connections, facade_scope
):
    configure_conversion(write_sql_connections)
    client = ConversionScopeClient()
    remote_original = list(client.remote_original)
    monkeypatch.setattr(reconfigure_api, "get_sql_connection", lambda _key: client)

    result = convert(facade_scope, return_metadata=True)

    assert client.rows(client.remote, client.source_table) == remote_original
    assert sorted(client.rows(client.coordinator, client.source_table)) == [(1,), (2,), (3,)]
    physical = [
        row
        for host in (client.coordinator, client.remote)
        for row in client.rows(host, "analytics.events_shard")
    ]
    assert sorted(physical) == [(1,), (2,), (3,)]
    assert result.metadata.source_rows == result.metadata.staged_rows == 3
    assert result.metadata.final_target_rows == 3
    assert result.metadata.row_count_validated is True
    assert result.data["cleanup_complete"] is True, result.data["cleanup_error"]
    assert result.data["cleanup_error"] is None
    assert len(result.data["backup_tables"]) == 1
    assert result.data["backup_table_scopes"] == [
        (table, None) for table in result.data["backup_tables"]
    ]
    source_rename = [
        statement
        for statement in client.commands
        if statement.startswith("RENAME TABLE analytics.events TO analytics.events__backup_")
    ]
    assert len(source_rename) == 1
    assert "ON CLUSTER" not in source_rename[0]
    source_cleanup = source_backup_cleanup(client.commands)
    assert len(source_cleanup) == 1, (
        "\n".join(client.commands) + f"\ncleanup_error={result.data['cleanup_error']}"
    )
    assert source_cleanup[0].args.get("cluster") is None
    assert all(
        client.rows(client.coordinator, table) == [] for table in result.data["backup_tables"]
    )


@pytest.mark.parametrize("facade_scope", [None, "coordinator_only"])
def test_conversion_scope_rolls_back_only_the_local_original_after_real_target_count_drift(
    monkeypatch, write_sql_connections, facade_scope
):
    configure_conversion(write_sql_connections)
    client = ConversionScopeClient()
    remote_original = list(client.remote_original)
    client.after_facade_cutover = lambda: client.insert_rows(
        client.coordinator, client.source_table, [(9999,)]
    )
    monkeypatch.setattr(reconfigure_api, "get_sql_connection", lambda _key: client)

    with pytest.raises(RuntimeError, match="row count changed during cutover"):
        convert(facade_scope)

    assert client.rows(client.coordinator, client.source_table) == [(1,), (2,), (3,)]
    assert client.rows(client.remote, client.source_table) == remote_original
    inverse = [
        statement
        for statement in client.commands
        if statement.startswith("RENAME TABLE analytics.events__backup_")
        and statement.endswith(" TO analytics.events")
    ]
    assert len(inverse) == 1
    assert "ON CLUSTER" not in inverse[0]
    assert all(drop.args.get("cluster") is None for drop in source_backup_cleanup(client.commands))


def test_conversion_scope_rejects_foreign_remote_facade_destination_before_any_ddl(
    monkeypatch, write_sql_connections
):
    configure_conversion(write_sql_connections)
    client = ConversionScopeClient()
    remote_original = list(client.remote_original)
    monkeypatch.setattr(reconfigure_api, "get_sql_connection", lambda _key: client)

    with pytest.raises(
        InvalidSqlInputError, match=r"[Ff]acade|[Ss]ource|[Dd]estination|[Ee]xisting"
    ):
        convert("core")

    assert client.commands == []
    assert client.rows(client.coordinator, client.source_table) == [(1,), (2,), (3,)]
    assert client.rows(client.remote, client.source_table) == remote_original


def test_conversion_scope_reports_local_backup_after_a_real_cutover_acknowledgement_loss(
    monkeypatch, write_sql_connections
):
    configure_conversion(write_sql_connections)
    client = ConversionScopeClient()
    remote_original = list(client.remote_original)

    def lose_ack():
        message = "The facade rename was applied, but its acknowledgement was lost."
        raise ConnectionError(message)

    client.after_facade_cutover = lose_ack
    monkeypatch.setattr(reconfigure_api, "get_sql_connection", lambda _key: client)

    with pytest.raises(StructuralCutoverError, match="backup scopes") as caught:
        convert(None)

    scopes = caught.value.analytics_toolkit_sql_backup_table_scopes
    assert len(scopes) == 1
    backup, scope = scopes[0]
    assert scope is None
    assert client.rows(client.coordinator, backup) == [(1,), (2,), (3,)]
    assert client.rows(client.remote, backup) == []
    assert client.rows(client.remote, client.source_table) == remote_original
    assert caught.value.analytics_toolkit_sql_retry_safe is False
    assert drop_commands(client.commands) == []
