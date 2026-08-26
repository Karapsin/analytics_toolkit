from __future__ import annotations

from tests.ab_utils._support.sql_native import (
    _plan_sql_native_bootstrap_batches,
    pytest,
)


@pytest.mark.parametrize(
    ("overrides", "error", "message"),
    [
        ({"large_source_row_threshold": 0}, ValueError, "row_threshold must be positive"),
        (
            {"large_source_resamples_per_query": 1.5},
            TypeError,
            "resamples_per_query must be an integer",
        ),
        ({"row_count": True}, TypeError, "row_count must be an integer"),
        ({"row_count": 0}, ValueError, "row_count must be positive"),
        ({"resamples": 1.5}, TypeError, "resamples must be an integer"),
        ({"resamples": 0}, ValueError, "resamples must be positive"),
    ],
)
def test_plan_sql_native_bootstrap_batches_rejects_invalid_options(
    overrides: dict[str, object],
    error: type[Exception],
    message: str,
) -> None:
    kwargs: dict[str, object] = {
        "row_count": 100,
        "resamples": 10,
        "large_source_row_threshold": 100_000,
        "large_source_resamples_per_query": 10,
    }
    kwargs.update(overrides)

    with pytest.raises(error, match=message):
        _plan_sql_native_bootstrap_batches(**kwargs)


def test_plan_sql_native_bootstrap_batches_uses_row_budget_and_large_cap() -> None:
    assert _plan_sql_native_bootstrap_batches(
        row_count=1_000,
        resamples=600,
        large_source_row_threshold=100_000,
        large_source_resamples_per_query=10,
    ) == [(1, 250), (251, 250), (501, 100)]
    assert _plan_sql_native_bootstrap_batches(
        row_count=100_000,
        resamples=25,
        large_source_row_threshold=100_000,
        large_source_resamples_per_query=10,
    ) == [(1, 10), (11, 10), (21, 5)]
    assert _plan_sql_native_bootstrap_batches(
        row_count=2_000_000,
        resamples=5,
        large_source_row_threshold=100_000,
        large_source_resamples_per_query=10,
    ) == [(1, 2), (3, 2), (5, 1)]
