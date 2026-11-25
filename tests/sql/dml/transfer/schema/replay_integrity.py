"""A corrupt complete checkpoint cannot leak a prefix to the target writer."""

from __future__ import annotations

import pytest
from analytics_toolkit.sql.dml.transfer.flow import adaptive_input, attempt

from tests.sql._support.adaptive_transfer import install, normalized_source_rows


def test_public_transfer_rejects_late_checkpoint_corruption_before_first_target_batch(monkeypatch):
    scenario = install(monkeypatch, rows=[(111,), (222,)], full_retry_cnt=1)
    original_run = adaptive_input.run_resolved_checkpoint_transfer

    def corrupt_then_run(options, refs, state, context):
        spool = options.input_resolution["spool"]
        assert spool.certified
        original = spool.path.read_bytes()
        assert original.count(b"222") == 1
        spool.path.write_bytes(original.replace(b"222", b"223"))
        return original_run(options, refs, state, context)

    def target_boundary(options, refs, state, *_args, **_kwargs):
        count = 0
        for batch in adaptive_input.resolved_source_batches(options, scenario.read):
            values = list(normalized_source_rows(options, refs, state, batch))
            scenario.loaded.append(values)
            count += len(values)
        return count

    monkeypatch.setattr(adaptive_input, "run_resolved_checkpoint_transfer", corrupt_then_run)
    monkeypatch.setattr(attempt, "load_stage_batches", target_boundary)

    with pytest.raises(ValueError, match="digest"):
        scenario.run()

    assert scenario.loaded == []
    assert scenario.finalizations == 0
    assert scenario.read_count == 1
