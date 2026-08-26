from __future__ import annotations

from tests.ab_utils._support.edge_cases import (
    np,
    pd,
    pytest,
    validation_module,
)


@pytest.mark.parametrize(
    ("alpha", "power", "message"),
    [
        (0.0, 0.8, "mde_alpha"),
        (0.05, 1.0, "mde_power"),
    ],
)
def test_validate_mde_parameters_rejects_boundary_values(
    alpha: float,
    power: float,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        validation_module._validate_mde_parameters(alpha, power)


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("null_user", "must not contain missing values"),
        ("duplicate_user", "must contain unique user ids"),
        ("null_group", "must not contain missing values"),
        ("missing_control", "was not found"),
        ("group_mismatch", "must match"),
    ],
)
def test_validate_pre_experiment_dataframe_rejects_invalid_pairs(
    case: str,
    message: str,
) -> None:
    experiment = pd.DataFrame({"user_id": [1, 2], "group_name": ["control", "control"]})
    pre_experiment = experiment.copy()
    if case == "null_user":
        pre_experiment.loc[1, "user_id"] = np.nan
    elif case == "duplicate_user":
        pre_experiment["user_id"] = [1, 1]
    elif case == "null_group":
        pre_experiment.loc[1, "group_name"] = None
    elif case == "missing_control":
        pre_experiment["group_name"] = "test"
    else:
        pre_experiment["group_name"] = ["test", "control"]

    with pytest.raises(ValueError, match=message):
        validation_module._validate_pre_experiment_dataframe(
            experiment,
            pre_experiment,
            group="group_name",
            control="control",
            user_id="user_id",
        )
