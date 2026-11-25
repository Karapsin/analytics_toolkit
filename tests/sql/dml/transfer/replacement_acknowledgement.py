"""A nontransactional cutover cannot replay after an unacknowledged rename."""

from __future__ import annotations

from copy import deepcopy

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.connection.errors import AmbiguousSqlReplaceError
from analytics_toolkit.sql.execution.backend_options import backend_operation
from sqlglot import exp, parse_one

from tests.sql._support.transfer_not_null import install_transfer_database


@pytest.mark.parametrize(
    "failed_rename", [1, 2], ids=["old-rename-lost-ack", "new-rename-lost-ack"]
)
def test_public_trino_replace_retains_all_recovery_data_after_lost_rename_acknowledgement(
    monkeypatch, failed_rename
):
    database = install_transfer_database(monkeypatch, "trino", not_null=())
    database.target.rows = [{"id": index, "payload": 100 + index} for index in range(1, 4)]
    sentinel = deepcopy(database.target)
    sentinel.rows = [{"id": index, "payload": index + 1000} for index in range(100)]
    database.tables[("sandbox", "unrelated")] = sentinel
    sentinel_before = deepcopy(sentinel.rows)
    old_rows = deepcopy(database.target.rows)
    renames = []
    failure = ConnectionError("Rename applied but acknowledgement was lost")
    execute = database.execute

    def execute_with_lost_ack(connection, statement, params=None):
        tree = parse_one(statement, read="trino")
        if not isinstance(tree, exp.Alter):
            return execute(connection, statement, params)
        database.commands.append(statement)
        source = database.identity(tree.this)
        destination = database.identity(tree.args["actions"][0].this)
        if destination in database.tables:
            message = "Rename destination already exists"
            raise ValueError(message)
        database.tables[destination] = database.tables.pop(source)
        renames.append((source, destination))
        if len(renames) == failed_rename:
            raise failure
        return [], []

    monkeypatch.setattr(database, "execute", execute_with_lost_ack)
    with backend_operation() as operation:
        with pytest.raises(AmbiguousSqlReplaceError) as caught:
            sql.transfer(
                "ch",
                "trino",
                from_sql="SELECT id, payload FROM source",
                to_table="sandbox.events",
                table_schema={"id": "BIGINT", "payload": "BIGINT"},
                write_mode="replace",
                ignore_source_staging=True,
                validate_row_count=False,
                retry_cnt=1,
                full_retry_cnt=3,
                timeout_increment=0,
                full_timeout_increment=0,
            )
        assert operation.structural_replay_blocked is True
        assert operation.preserve_recovery is True

    assert caught.value.__cause__ is failure
    steps = caught.value.analytics_toolkit_sql_cutover_journal["steps"]
    assert len(steps) == 2
    for index, step in enumerate(steps):
        assert step["submitted"] is (index < failed_rename)
        assert step["acknowledged"] is (index < failed_rename - 1)
        assert step["rollback_submitted"] is False
        assert step["inverse"]
    assert len(renames) == failed_rename
    assert len(database.reads) == 1
    backups = [table for (_schema, name), table in database.tables.items() if "__backup_" in name]
    assert len(backups) == 1
    assert backups[0].rows == old_rows
    payloads = [
        table
        for (_schema, name), table in database.tables.items()
        if "__replace_" in name or name == "events"
    ]
    assert any(
        table.rows == [{"id": 7, "payload": 11}, {"id": 8, "payload": None}] for table in payloads
    )
    assert database.tables[("sandbox", "unrelated")] is sentinel
    assert sentinel.rows == sentinel_before
    assert not any(statement.startswith("DROP TABLE") for statement in database.commands)
