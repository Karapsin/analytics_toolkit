"""A new transfer cannot acquire older resources through their reserved names."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.dml.transfer.flow.stage_identity import resolve_destination_identity
from sqlglot import exp, parse_one

from tests.conftest import DEFAULT_SQL_CONNECTIONS
from tests.sql._support.load_column_state import ExistingColumnStore
from tests.sql._support.transfer_native_assignment import (
    NativeTransferConnection,
    install_native_transfer,
)


@pytest.mark.parametrize("suffix", ["upsert", "source", "w00000a1b2"])
def test_public_transfer_retains_unproved_prior_run_recovery_candidates(
    monkeypatch, write_sql_connections, capsys, suffix
):
    configs = deepcopy(DEFAULT_SQL_CONNECTIONS)
    configs["gp"]["transfer_staging_schema"] = "sandbox"
    write_sql_connections(configs)
    database = install_native_transfer(
        monkeypatch,
        rows=[(201,), (202,)],
        target_schema={"payload": "BIGINT"},
        source_types={"payload": "Int64"},
    )
    digest = resolve_destination_identity("sandbox.events", "gp").hash_prefix
    name = f"{digest}__{'b' * 32}__{suffix}"
    retained = ExistingColumnStore("gp", ["payload"], not_null=())
    retained.schema = {"payload": "BIGINT"}
    retained.rows = [{"payload": value} for value in range(1000, 1100)]
    database.tables[("sandbox", name)] = retained
    before = deepcopy(retained.rows)
    original_rows = deepcopy(database.target.rows)
    metadata = database.metadata

    def listing(statement, params):
        if "information_schema.tables" in statement and "table_name LIKE" in statement:
            assert params == ("sandbox", digest + "__%")
            return [(name,)]
        return metadata(statement, params)

    monkeypatch.setattr(database, "metadata", listing)

    def source_count(_connection, statement):
        tree = parse_one(statement, read="clickhouse")
        assert isinstance(tree, exp.Select)
        assert tree.find(exp.Count) is not None
        assert any(table.name == "source" for table in tree.find_all(exp.Table))
        return SimpleNamespace(result_rows=[(len(database.source_rows),)])

    monkeypatch.setattr(NativeTransferConnection, "query", source_count, raising=False)
    result = sql.transfer(
        "ch",
        "gp",
        from_sql="SELECT payload FROM source",
        to_table="sandbox.events",
        write_mode="append",
        ignore_source_staging=True,
        retry_cnt=1,
        full_retry_cnt=1,
        timeout_increment=0,
        full_timeout_increment=0,
    )

    assert result == 2
    assert database.tables[("sandbox", name)].rows == before
    assert database.tables[("sandbox", name)].schema == retained.schema
    assert database.target.rows == [*original_rows, {"payload": 201}, {"payload": 202}]
    assert len(database.reads) == 1
    assert name in capsys.readouterr().out
    assert set(database.tables) == {("sandbox", "events"), ("sandbox", name)}
