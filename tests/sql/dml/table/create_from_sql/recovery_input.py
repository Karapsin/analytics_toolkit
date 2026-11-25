"""Failed destructive query publication retains its certified original input."""

from __future__ import annotations

import shutil
from copy import deepcopy

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.core.replay_spool import ReplaySpool

from tests.conftest import DEFAULT_SQL_CONNECTIONS
from tests.sql._support.adaptive_transfer import track_spools
from tests.sql._support.query_native_assignment import install_query_native


@pytest.mark.parametrize("failure_type", [ConnectionError, KeyboardInterrupt])
def test_public_self_replacement_retains_certified_input_after_terminal_publication_failure(
    monkeypatch, write_sql_connections, capsys, failure_type
):
    configs = deepcopy(DEFAULT_SQL_CONNECTIONS)
    configs["gp"]["transfer_staging_schema"] = None
    write_sql_connections(configs)
    rows = [(1,), (2,)]
    database, _aliases, _connect = install_query_native(monkeypatch, rows, native_type="BIGINT")
    paths = track_spools(monkeypatch)
    failure = failure_type("Publication stopped after source replacement")
    submissions = []
    insert = database._insert

    def fail_target_population(tree, identity, params):
        if identity == ("sandbox", "events"):
            submissions.append(tree.copy())
            raise failure
        return insert(tree, identity, params)

    monkeypatch.setattr(database, "_insert", fail_target_population)
    try:
        with pytest.raises(failure_type, match="Publication stopped") as caught:
            sql.create_table(
                "gp",
                "sandbox.events",
                sql="SELECT payload FROM sandbox.events",
                insert_data=True,
                drop_if_exists=True,
                retry_cnt=2,
                timeout_increment=0,
            )

        assert caught.value is failure
        assert submissions
        assert database.python_selected_rows == rows
        assert len(database.python_reads) == 1
        assert len(paths) == 1
        manifest = paths[0].parent / "manifest.json"
        assert manifest.is_file(), "Certified self-source input was discarded after mutation"
        recovered = ReplaySpool.open_retained(manifest)
        try:
            assert [row for batch in recovered.iter_batches(1) for row in batch.rows] == rows
            assert recovered.row_count == 2
        finally:
            recovered.close()
        assert "Retained certified query input checkpoint" in capsys.readouterr().out
        assert database.opened
        assert len(database.closed) == len(database.opened)
    finally:
        for path in paths:
            shutil.rmtree(path.parent, ignore_errors=True)
