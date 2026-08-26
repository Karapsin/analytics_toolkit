from __future__ import annotations

from tests.ab_utils._support.edge_cases import (
    pytest,
    sql_bootstrap_module,
    validation_module,
)


def test_sql_bootstrap_merges_repeated_metric_discard_counts() -> None:
    accumulators = {
        ("metric", "a", "control"): {"requested": 10.0, "family_n": 8.0},
        ("metric", "b", "control"): {"requested": 12.0, "family_n": 7.0},
    }
    observed = {
        ("metric", "a", "control"): (1.0, 1.0),
        ("metric", "b", "control"): (1.0, 1.0),
    }
    with pytest.warns(RuntimeWarning, match="discarded 5 of 12"):
        sql_bootstrap_module._warn_discarded_sql_native_bootstrap_replicates(
            accumulators, observed_statistics=observed
        )


@pytest.mark.parametrize(
    ("resamples", "error", "message"),
    [
        (True, TypeError, "must be an integer"),
        (0, ValueError, "must be positive"),
    ],
)
def test_validate_multiple_comparisons_rejects_invalid_resamples(
    resamples: object,
    error: type[Exception],
    message: str,
) -> None:
    with pytest.raises(error, match=message):
        validation_module._validate_multiple_comparisons_parameters(
            multiple_comparisons_adjustment=True,
            multiple_comparisons_adjustment_resamples=resamples,
            bootstrap_random_state=0,
            bootstrap_n_jobs=1,
            bootstrap_progress=False,
        )
