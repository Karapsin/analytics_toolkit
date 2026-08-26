from __future__ import annotations

from tests.ab_utils._support.sql_native import (
    _compact_bootstrap_summary,
    _reduce_sql_native_bootstrap_batches,
    math,
    pd,
    pytest,
    warnings,
)


def test_reduce_sql_native_bootstrap_batches_merges_moments_and_plus_one() -> None:
    columns = {
        "metric_name": "orders",
        "group_1": "test",
        "group_2": "control",
        "requested_resamples": 2,
        "valid_family_resamples": 2,
        "valid_delta_resamples": 2,
        "max_t_exceedances": 1,
    }
    first = pd.DataFrame([{**columns, "delta_mean": 2.0, "delta_m2": 2.0}])
    second = pd.DataFrame(
        [
            {
                **columns,
                "valid_family_resamples": 1,
                "delta_mean": 6.0,
                "delta_m2": 2.0,
            }
        ]
    )

    with pytest.warns(RuntimeWarning, match="discarded 1 of 4"):
        result = _reduce_sql_native_bootstrap_batches(
            batches=[(2, first), (2, second)],
            observed_statistics={("orders", "test", "control"): (1.0, 0.5)},
        )

    assert result.loc[0, "se_bootstrap"] == pytest.approx(math.sqrt(20 / 3))
    assert result.loc[0, "bootstrap_adj_p"] == pytest.approx(3 / 4)


def test_reduce_sql_native_bootstrap_batches_requires_every_expected_key() -> None:
    columns = [
        "metric_name",
        "group_1",
        "group_2",
        "requested_resamples",
        "valid_family_resamples",
        "valid_delta_resamples",
        "delta_mean",
        "delta_m2",
        "max_t_exceedances",
    ]

    with pytest.raises(ValueError, match="missing expected key"):
        _reduce_sql_native_bootstrap_batches(
            batches=[(2, pd.DataFrame(columns=columns))],
            observed_statistics={("orders", "test", "control"): (1.0, 0.5)},
        )


def test_reduce_sql_native_bootstrap_does_not_warn_without_observed_family() -> None:
    summary = pd.DataFrame(
        [
            {
                "metric_name": "orders",
                "group_1": "test",
                "group_2": "control",
                "requested_resamples": 2,
                "valid_family_resamples": 0,
                "valid_delta_resamples": 0,
                "delta_mean": None,
                "delta_m2": 0.0,
                "max_t_exceedances": 0,
            }
        ]
    )

    with warnings.catch_warnings(record=True) as caught:
        result = _reduce_sql_native_bootstrap_batches(
            batches=[(2, summary)],
            observed_statistics={("orders", "test", "control"): (math.nan, math.nan)},
        )

    assert caught == []
    assert math.isnan(result.loc[0, "bootstrap_adj_p"])


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("missing_column", "missing column"),
        ("duplicate", "duplicate key"),
        ("unexpected_key", "unexpected key"),
        ("requested_resamples", "requested_resamples is inconsistent"),
        ("valid_family_too_large", "counts are inconsistent"),
        ("exceedances_too_large", "counts are inconsistent"),
        ("negative_m2", "delta_m2 must be non-negative"),
        ("null_count", "count must not be null"),
        ("fractional_count", "count must be an integer"),
        ("negative_count", "count must be non-negative"),
        ("infinite_mean", "delta_mean must be finite"),
        ("nan_m2", "delta_m2 must be finite"),
    ],
)
def test_reduce_sql_native_bootstrap_rejects_malformed_summaries(
    case: str,
    message: str,
) -> None:
    if case == "missing_column":
        frame = _compact_bootstrap_summary().drop(columns=["delta_m2"])
    elif case == "duplicate":
        summary = _compact_bootstrap_summary()
        frame = pd.concat([summary, summary])
    else:
        overrides = {
            "unexpected_key": {"metric_name": "unexpected"},
            "requested_resamples": {"requested_resamples": 3},
            "valid_family_too_large": {"valid_family_resamples": 3},
            "exceedances_too_large": {"max_t_exceedances": 3},
            "negative_m2": {"delta_m2": -1},
            "null_count": {"valid_family_resamples": None},
            "fractional_count": {"valid_family_resamples": 1.5},
            "negative_count": {"valid_family_resamples": -1},
            "infinite_mean": {"delta_mean": math.inf},
            "nan_m2": {"delta_m2": math.nan},
        }
        frame = _compact_bootstrap_summary(**overrides[case])

    with pytest.raises(ValueError, match=message):
        _reduce_sql_native_bootstrap_batches(
            batches=[(2, frame)],
            observed_statistics={("orders", "test", "control"): (1.0, 0.5)},
        )
