"""Integrity certification precedes exposing any part of a replay generation."""

from __future__ import annotations

import pytest
from analytics_toolkit.sql.core.replay_spool import ReplaySpool


@pytest.mark.parametrize("batch", [False, True])
def test_modified_late_replay_payload_is_rejected_before_yielding_a_prefix(batch):
    with ReplaySpool(["private"]) as spool:
        spool.write_rows([(111,), (222,)])
        spool.certify()
        original = spool.path.read_bytes()
        assert original.count(b"222") == 1
        spool.path.write_bytes(original.replace(b"222", b"223"))
        rows = spool.iter_batches(1) if batch else spool.iter_rows()

        with pytest.raises(ValueError, match="digest") as error:
            next(rows)

        assert "111" not in str(error.value)
        assert "222" not in str(error.value)
        assert "223" not in str(error.value)
