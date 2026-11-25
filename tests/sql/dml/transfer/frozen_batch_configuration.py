"""A lost acknowledgement must not replay a frozen batch in a changed scope."""

from __future__ import annotations

import json
from copy import deepcopy

import pytest
from analytics_toolkit.general import set_connections_path
from analytics_toolkit.sql.connection.config import get_connection_config
from analytics_toolkit.sql.core.replay_spool import FrozenInputIdentity, ReplaySpool
from analytics_toolkit.sql.core.type_resolution import ResolvedColumn
from analytics_toolkit.sql.dml.load.load_sql_table import insert_rows_batch
from analytics_toolkit.sql.dml.transfer.flow.adaptive_input import (
    install_configuration_guard,
    resolved_source_batches,
)
from analytics_toolkit.sql.dml.transfer.flow.native_assignment import native_insert_retry_arguments
from analytics_toolkit.sql.dml.transfer.runtime.models import TransferOptions
from analytics_toolkit.sql.dml.transfer.runtime.retry import run_with_retry

from tests.conftest import DEFAULT_SQL_CONNECTIONS


@pytest.mark.parametrize("change_selection", [False, True], ids=["same-scope", "changed-scope"])
def test_frozen_transfer_batch_checks_selected_configuration_before_inner_retry(
    tmp_path, write_sql_connections, change_selection
):
    configs = deepcopy(DEFAULT_SQL_CONNECTIONS)
    configs["ch_target"] = {**configs["ch"], "database": "frozen_target"}
    original_path = write_sql_connections(configs)
    alternate = deepcopy(configs)
    alternate["ch_target"]["database"] = "changed_target"
    alternate_directory = tmp_path / "alternate-project"
    alternate_directory.mkdir()
    alternate_path = alternate_directory / ".connections"
    alternate_path.write_text(json.dumps(alternate), encoding="utf-8")
    options = TransferOptions(
        from_db_key="ch",
        from_db_backend="ch",
        to_db_key="ch_target",
        to_db_backend="ch",
        source_sql="SELECT id FROM source",
        target_table="sandbox.target",
        batch_size=2,
    )
    install_configuration_guard(options)
    identity = FrozenInputIdentity(
        configuration_digest=options.input_resolution["configuration_digest"]
    )
    spool = ReplaySpool(["id"], identity=identity)
    spool.write_rows([(1,), (2,)])
    spool.certify()
    options.input_resolution.update(
        spool=spool,
        resolutions=[ResolvedColumn("Int64", target_backend="ch")],
    )
    opened_config = get_connection_config("ch_target")
    assert opened_config.database == "frozen_target"
    submissions = []

    class OpenedNativeConnection:
        is_native_transport = True

        def insert(self, *, table, data, column_names, column_type_names):
            assert table == "sandbox.private_checkpoint"
            assert column_names == ["id"]
            assert column_type_names == ["Int64"]
            submissions.append((opened_config.database, tuple(data)))
            if len(submissions) == 1:
                if change_selection:
                    set_connections_path(alternate_path)
                    assert get_connection_config("ch_target").database == "changed_target"
                message = "lost insert acknowledgement"
                raise ConnectionError(message)

    try:
        batch = next(resolved_source_batches(options, None))

        def write():
            return insert_rows_batch(
                "ch",
                {"connection": OpenedNativeConnection()},
                "sandbox.private_checkpoint",
                batch.columns,
                batch.rows,
                retry_cnt=3,
                timeout_increment=0,
                target_column_types={"id": "Int64"},
                connection_key="ch_target",
                **native_insert_retry_arguments(options, run_with_retry),
            )

        if change_selection:
            with pytest.raises(ValueError, match="configuration differs"):
                write()
            assert submissions == [("frozen_target", ((1,), (2,)))]
        else:
            assert write() == 2
            assert submissions == [("frozen_target", ((1,), (2,)))] * 2
        assert spool.certified
        assert spool.row_count == 2
    finally:
        set_connections_path(original_path)
        spool.close()
