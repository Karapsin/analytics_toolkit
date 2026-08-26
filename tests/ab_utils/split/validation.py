from __future__ import annotations

from tests.ab_utils._support.edge_cases import (
    Any,
    math,
    np,
    pd,
    pytest,
    split_module,
)


@pytest.mark.parametrize(
    ("call", "error", "message"),
    [
        (lambda: split_module._build_group_names(True), TypeError, "must be an integer"),
        (
            lambda: split_module._normalize_group_ratios(50, expected_size=2),
            TypeError,
            "must be a sequence",
        ),
        (
            lambda: split_module._normalize_stratification_cols(b"segment"),
            TypeError,
            "string or a sequence",
        ),
        (
            lambda: split_module._normalize_stratification_cols(7),
            TypeError,
            "string or a sequence",
        ),
        (
            lambda: split_module._normalize_stratification_cols(["segment", 1]),
            TypeError,
            "only strings",
        ),
        (
            lambda: split_module._validate_split_dataframe(
                [],
                split_col="user_id",
                stratification_cols=[],
            ),
            TypeError,
            "pandas DataFrame",
        ),
        (
            lambda: split_module._validate_split_dataframe(
                pd.DataFrame({"user_id": [1]}),
                split_col=1,
                stratification_cols=[],
            ),
            TypeError,
            "split_col must be a string",
        ),
        (lambda: split_module._validate_group_col(1), TypeError, "must be a string"),
        (
            lambda: split_module._validate_group_col("is_mandatory_user"),
            ValueError,
            "conflicts",
        ),
        (lambda: split_module._validate_random_state(True), TypeError, "integer or None"),
        (
            lambda: split_module._normalize_target_sample_size(True, max_size=3),
            TypeError,
            "integer or None",
        ),
        (
            lambda: split_module._normalize_target_sample_size(0, max_size=3),
            ValueError,
            "must be positive",
        ),
        (
            lambda: split_module._normalize_mandatory_users_group(1, ["control", "test_1"]),
            TypeError,
            "must be a string",
        ),
    ],
)
def test_split_helpers_validate_types_and_reserved_names(
    call: Any,
    error: type[Exception],
    message: str,
) -> None:
    with pytest.raises(error, match=message):
        call()


def test_split_none_defaults_return_full_sample_contracts() -> None:
    assert split_module._normalize_stratification_cols(None) == []
    assert split_module._validate_random_state(None) is None
    assert split_module._normalize_target_sample_size(None, max_size=3) == 3
    assert split_module._normalize_stratification_cols(("segment", "country")) == [
        "segment",
        "country",
    ]


@pytest.mark.parametrize(
    ("mandatory_df", "error", "message"),
    [
        ([], TypeError, "pandas DataFrame"),
        (pd.DataFrame({"user_id": [1, np.nan]}), ValueError, "missing values"),
    ],
)
def test_mandatory_position_validation_rejects_invalid_frames(
    mandatory_df: object,
    error: type[Exception],
    message: str,
) -> None:
    with pytest.raises(error, match=message):
        split_module._get_present_mandatory_positions(
            mandatory_users_df=mandatory_df,
            split_col="user_id",
            id_to_position={1: 0},
        )


def test_missing_stratum_detection_handles_scalar_errors_and_array_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raise_type_error(_value: object) -> bool:
        raise TypeError

    monkeypatch.setattr(split_module.pd, "isna", raise_type_error)
    assert split_module._is_missing_stratum_value(object()) is False

    monkeypatch.setattr(split_module.pd, "isna", lambda _value: np.array([True]))
    assert split_module._is_missing_stratum_value(object()) is False


def test_sample_and_assignment_helpers_handle_empty_and_invalid_counts() -> None:
    rng = np.random.default_rng(0)
    assert (
        split_module._sample_positions_by_strata(
            [0],
            strata_keys=[("all",)],
            sample_size=0,
            rng=rng,
        )
        == []
    )
    with pytest.raises(ValueError, match="cannot exceed"):
        split_module._sample_positions_by_strata(
            [0],
            strata_keys=[("all",)],
            sample_size=2,
            rng=rng,
        )
    with pytest.raises(ValueError, match="must sum"):
        split_module._assign_positions_to_groups(
            [0],
            group_names=["control"],
            group_counts=[0],
            strata_keys=[("all",)],
            rng=rng,
        )
    assert (
        split_module._assign_positions_to_groups(
            [],
            group_names=["control"],
            group_counts=[0],
            strata_keys=[],
            rng=rng,
        )
        == {}
    )


def test_stratified_count_helpers_fill_or_reject_deficits() -> None:
    rng = np.random.default_rng(0)
    assert (
        split_module._build_stratified_count_matrix(
            stratum_sizes=[],
            group_counts=[0, 0],
            rng=rng,
        )
        == []
    )

    counts = np.zeros((2, 2), dtype=int)
    row_deficits = np.array([1, 1])
    column_deficits = np.array([1, 1])
    split_module._fill_remaining_matrix_deficits(
        floor_counts=counts,
        row_deficits=row_deficits,
        column_deficits=column_deficits,
        rng=rng,
    )
    assert counts.sum(axis=1).tolist() == [1, 1]
    assert counts.sum(axis=0).tolist() == [1, 1]

    with pytest.raises(ValueError, match="Unable to build"):
        split_module._fill_remaining_matrix_deficits(
            floor_counts=np.zeros((1, 1), dtype=int),
            row_deficits=np.array([1]),
            column_deficits=np.array([0]),
            rng=rng,
        )


@pytest.mark.parametrize(
    ("total", "weights", "expected", "message"),
    [
        (-1, [1.0], None, "non-negative"),
        (1, [], None, "must not be empty"),
        (1, [-1.0], None, "non-negative finite"),
        (1, [math.inf], None, "non-negative finite"),
        (1, [0.0], None, "positive sum"),
        (0, [], [], None),
    ],
)
def test_round_counts_validates_degenerate_weights(
    total: int,
    weights: list[float],
    expected: list[int] | None,
    message: str | None,
) -> None:
    if message is None:
        assert split_module._round_counts(total, weights, rng=np.random.default_rng(0)) == expected
        return
    with pytest.raises(ValueError, match=message):
        split_module._round_counts(total, weights, rng=np.random.default_rng(0))


def test_fit_counts_to_capacities_redistributes_and_detects_shortfall() -> None:
    rng = np.random.default_rng(0)
    assert split_module._fit_counts_to_capacities(
        [5, 0],
        capacities=[1, 5],
        total=5,
        rng=rng,
    ) == [1, 4]
    assert split_module._fit_counts_to_capacities(
        [1, 1],
        capacities=[1, 1],
        total=2,
        rng=rng,
    ) == [1, 1]
    one_slot = split_module._fit_counts_to_capacities(
        [0, 0],
        capacities=[1, 1],
        total=1,
        rng=rng,
    )
    assert sum(one_slot) == 1
    assert all(count >= 0 for count in one_slot)
    with pytest.raises(ValueError, match="Unable to fit"):
        split_module._fit_counts_to_capacities(
            [0],
            capacities=[0],
            total=1,
            rng=rng,
        )


def test_take_random_positions_handles_zero_and_oversized_requests() -> None:
    rng = np.random.default_rng(0)
    assert split_module._take_random_positions([1, 2], 0, rng=rng) == []
    with pytest.raises(ValueError, match="cannot exceed"):
        split_module._take_random_positions([1], 2, rng=rng)


def test_do_split_rejects_non_boolean_compensation() -> None:
    with pytest.raises(TypeError, match="must be a boolean"):
        split_module.do_split(
            pd.DataFrame({"user_id": [1, 2]}),
            compensate_mandatory_users=1,
        )
