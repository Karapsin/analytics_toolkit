"""Business-tab routing and grouped chart contracts reject ambiguous recipes."""

import copy
from unittest.mock import patch

import pytest
from analytics_toolkit.datalens_utils.dashboard_generation.dashboard import configuration
from analytics_toolkit.datalens_utils.errors import DataLensUtilsError
from analytics_toolkit.datalens_utils.settings import read_config


@pytest.mark.parametrize(
    "problem",
    [
        "duplicate",
        "tab",
        "empty",
        "no default",
        "two defaults",
        "unknown chart",
        "cross tab",
        "alias parameter",
        "alias field",
        "alias duplicate",
        "layout",
    ],
)
def test_invalid_business_recipe_stops_before_writes(financial_case, problem):  # noqa: C901, PLR0912
    case = financial_case
    case.configuration_reads.stop()
    values = {}
    for relative in ("UI/chart_groups.json", "UI/links/aliases.json", "UI/layout.json"):
        values[relative] = copy.deepcopy(read_config(relative))
    values["UI/layout.json"]["month"] = case.fixture_layout["month"]
    groups = values["UI/chart_groups.json"]
    group = next(iter(groups.values()))
    if problem == "duplicate":
        groups["duplicate"] = copy.deepcopy(group)
    elif problem == "tab":
        group["tab"] = "unknown"
    elif problem == "empty":
        group["charts"] = []
    elif problem == "no default":
        for member in group["charts"]:
            member["default"] = False
    elif problem == "two defaults":
        for member in group["charts"][:2]:
            member["default"] = True
    elif problem in {"unknown chart", "cross tab"}:
        group["charts"][0]["key"] = "unknown" if problem == "unknown chart" else "month_summary"
    elif problem.startswith("alias"):
        aliases = values["UI/links/aliases.json"]["month"]
        if problem == "alias parameter":
            aliases.append([{"parameter": "unknown"}, {"parameter": "report_period"}])
        elif problem == "alias field":
            aliases.append(
                [{"dataset": "month", "field": "unknown"}, {"parameter": "report_period"}]
            )
        else:
            aliases.append([{"parameter": "report_period"}] * 2)
    else:
        values["UI/layout.json"]["dynamics"]["unknown"] = [0, 400, 1, 1]
    with patch.object(
        configuration, "read_config", side_effect=lambda name: values.get(name, read_config(name))
    ), pytest.raises(DataLensUtilsError):
        configuration.read_contents()
    assert case.backend.writes == []


@pytest.mark.parametrize("family", ["wizard", "ql", "editor"])
@pytest.mark.parametrize("kind", ["dataset", "manual"])
@pytest.mark.parametrize("problem", ["cross tab", "missing parameter", "valid"])
def test_selector_group_receivers_validate_parameter_contracts(family, kind, problem):
    selector = {
        "key": "sender",
        "tab": "business",
        "title": "Period",
        "source": {"kind": kind, "dataset": "sales", "field": "Period", "param_name": "period"},
        "control": {"element": "select"},
        "recipients": ["group"],
    }
    inputs = {
        "tabs": {"business": {}, "other": {}},
        "charts": {
            "chart": {
                "family": family,
                "params": [{"name": "period"}],
                "dataset": "sales",
                "tab": "business",
            }
        },
        "datasets": {"sales": {"fields": {"Period": {}}, "parameters": {"period": {}}}},
        "chart_groups": {
            "group": {
                "tab": "other" if problem == "cross tab" else "business",
                "charts": [{"key": "chart"}],
            }
        },
    }
    if problem == "missing parameter":
        inputs["datasets"]["sales"]["parameters"] = {}
        inputs["charts"]["chart"]["params"] = []
    if problem == "cross tab" or (problem == "missing parameter" and kind == "manual"):
        with pytest.raises(DataLensUtilsError):
            configuration._validate_selector("sender", selector, **inputs)
    else:
        configuration._validate_selector("sender", selector, **inputs)


def test_manual_parameter_must_exist_on_dependent_dataset_selector():
    selector = {
        "tab": "business",
        "title": "Period",
        "source": {"kind": "manual", "param_name": "period"},
        "control": {"element": "select"},
        "recipients": ["date"],
    }
    with pytest.raises(DataLensUtilsError, match="not declared"):
        configuration._validate_selector(
            "sender",
            selector,
            tabs={"business": {}},
            charts={},
            datasets={"sales": {}},
            selectors={
                "date": {"tab": "business", "source": {"kind": "dataset", "dataset": "sales"}}
            },
        )
