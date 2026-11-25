"""A successful retry still reports an earlier unacknowledged source stage."""

from __future__ import annotations

from copy import deepcopy

from analytics_toolkit import sql

from tests.conftest import DEFAULT_SQL_CONNECTIONS
from tests.sql._support.query_native_assignment import QueryNativeConnection
from tests.sql._support.query_session_namespace import install_session_query


def test_public_script_success_reports_its_earlier_unacknowledged_source_candidate(
    monkeypatch, write_sql_connections, capsys
):
    configs = deepcopy(DEFAULT_SQL_CONNECTIONS)
    configs["gp"]["transfer_staging_schema"] = "scratch"
    write_sql_connections(configs)
    database = install_session_query(monkeypatch)
    original = deepcopy(database.tables[("archive", "source_rows")].rows)
    public = deepcopy(database.tables[("public", "events")].rows)
    commit = QueryNativeConnection.commit
    lost = []

    def lose_first_source_ack(connection):
        commit(connection)
        if database.snapshot_creates and not lost:
            lost.append(database.snapshot_creates[-1])
            message = "Source CTAS committed but its acknowledgement was lost"
            raise ConnectionError(message)

    monkeypatch.setattr(QueryNativeConnection, "commit", lose_first_source_ack)
    result = sql.create_table(
        "gp",
        "archive.created",
        sql="SET search_path TO archive; SELECT payload FROM archive.source_rows",
        insert_data=True,
        retry_cnt=2,
        timeout_increment=0,
        return_metadata=True,
    )

    assert result.rows == 2
    assert len(lost) == 1
    assert database.tables[lost[0]].rows == original
    assert database.tables[("archive", "created")].rows == original
    assert database.tables[("archive", "source_rows")].rows == original
    assert database.tables[("public", "events")].rows == public
    assert len(database.snapshot_creates) == database.selected_reads == 2
    assert database.snapshot_creates[1] not in database.tables
    output = capsys.readouterr().out
    assert lost[0][1] in output
    assert "unacknowledged" in output.lower()
    assert len(database.closed) == len(database.opened)
