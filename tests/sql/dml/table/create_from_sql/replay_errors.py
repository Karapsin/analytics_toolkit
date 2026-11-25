"""Frozen CH publication preserves deterministic classification and private logs."""

from __future__ import annotations

import struct

import pytest
from analytics_toolkit.sql.core import replay_spool
from analytics_toolkit.sql.dml.load.load_sql_table import AmbiguousTableLoadError

from tests.sql._support.query_client_validation import create, install_query_client
from tests.sql.backends.ch._support.network_transport import encode_normalized


class DeterministicWriteError(RuntimeError):
    code = 53
    error_name = "TYPE_MISMATCH"


@pytest.fixture
def replay_observation(capsys, caplog, tmp_path):
    return capsys, caplog, tmp_path


@pytest.mark.parametrize("transport", ["native", "http"])
def test_public_frozen_ch_replay_deterministic_error_is_not_repeated_or_logged(
    monkeypatch, write_sql_connections, replay_observation, transport
):
    capsys, caplog, tmp_path = replay_observation
    monkeypatch.setattr(replay_spool.tempfile, "tempdir", str(tmp_path))
    client = install_query_client(
        monkeypatch, write_sql_connections, transport, "Int256", [1, 2], exists=True
    )
    failure = DeterministicWriteError("private-row-and-driver-details-201202")
    submissions = []

    def rejected_insert(table, data, *, column_names, column_type_names, **_kwargs):
        assert table == "analytics.events"
        assert column_names == ["payload"]
        assert column_type_names == ["Int64"]
        rows = list(data)
        wire = encode_normalized("Int64", [row[0] for row in rows], transport)
        submissions.append((rows, wire))
        raise failure

    monkeypatch.setattr(client, "insert", rejected_insert)
    with pytest.warns(
        RuntimeWarning,
        match="Could not remove partial target.*ownership is unverified",
    ), pytest.raises(AmbiguousTableLoadError) as caught:
        create.create_table_from_sql(
            "ch",
            "analytics.events",
            "SELECT payload FROM analytics.events",
            table_schema={"payload": "Int64"},
            drop_target_if_exists=True,
            retry_cnt=3,
            timeout_increment=0,
        )

    assert caught.value.__cause__ is failure
    assert submissions == [([(1,), (2,)], struct.pack("<2q", 1, 2))]
    assert client.scans == [True]
    assert client.rows == []
    assert client.physical_type == "Int64"
    assert client.close_calls == len(client.opens)
    output = capsys.readouterr()
    assert str(failure) not in output.out + output.err + caplog.text
