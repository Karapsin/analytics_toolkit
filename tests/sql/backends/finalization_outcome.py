"""Finalization counts belong to the caller whose operation produced them."""

from __future__ import annotations

import pytest
from analytics_toolkit.sql.backends.finalization_outcome import (
    capture_finalization_outcome,
    record_final_target_rows,
)


def _finalize_target(rows):
    if rows is not None:
        record_final_target_rows(rows)


def _run_finalizer(rows):
    with capture_finalization_outcome() as outcome:
        result = _finalize_target(rows)
    return result, outcome


@pytest.mark.parametrize("child_fails", [False, True], ids=["child-success", "child-error"])
def test_nested_finalization_restores_the_exact_parent_count_scope(child_fails):
    child_outcomes = []
    primary = RuntimeError("nested finalization failed after its acknowledged work")

    def finalize_nested_target():
        with capture_finalization_outcome() as child:
            child_outcomes.append(child)
            _finalize_target(7)
            if child_fails:
                raise primary

    with capture_finalization_outcome() as parent:
        _finalize_target(41)
        if child_fails:
            with pytest.raises(RuntimeError) as failure:
                finalize_nested_target()
            assert failure.value is primary
        else:
            finalize_nested_target()
        assert parent.final_target_rows == 41
        assert child_outcomes[0].final_target_rows == 7
        _finalize_target(42)

    assert parent.final_target_rows == 42
    assert child_outcomes[0].final_target_rows == 7
    _finalize_target(999)
    assert parent.final_target_rows == 42
    assert child_outcomes[0].final_target_rows == 7
    result, next_operation = _run_finalizer(None)
    assert result is None
    assert next_operation.final_target_rows is None


def test_failed_finalization_releases_its_capture_before_the_next_operation():
    failed_outcomes = []
    primary = RuntimeError("operation failed after target finalization")

    def fail_after_finalizing():
        with capture_finalization_outcome() as failed:
            failed_outcomes.append(failed)
            _finalize_target(12)
            raise primary

    with pytest.raises(RuntimeError) as failure:
        fail_after_finalizing()

    assert failure.value is primary
    _finalize_target(999)
    result, next_operation = _run_finalizer(None)
    assert result is None
    assert next_operation.final_target_rows is None
    assert failed_outcomes[0].final_target_rows == 12


def test_empty_acknowledged_target_remains_distinct_from_unknown_and_unscoped_work():
    _finalize_target(999)
    empty_result, empty_operation = _run_finalizer(0)
    _finalize_target(1000)
    unknown_result, unknown_operation = _run_finalizer(None)

    assert empty_result is unknown_result is None
    assert empty_operation.final_target_rows == 0
    assert unknown_operation.final_target_rows is None
