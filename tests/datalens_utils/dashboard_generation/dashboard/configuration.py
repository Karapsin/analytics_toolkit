"""Native selector contracts, unique layout identity, and link validation."""

import copy
import json
from unittest.mock import patch

import pytest
from analytics_toolkit.datalens_utils.dashboard_generation.dashboard import configuration as config
from analytics_toolkit.datalens_utils.errors import DataLensUtilsError


def selector(**changes):
    return {
        "key": "filter",
        "tab": "wizard",
        "title": "Region",
        "source": {"kind": "dataset", "dataset": "sales", "field": "Region"},
        "recipients": ["trend"],
        "control": {"element": "select"},
        **changes,
    }


def inputs():
    return {
        "tabs": {"wizard": {}, "ql": {}, "editor": {}},
        "charts": {
            "trend": {"family": "wizard", "type": "line"},
            "query": {"family": "ql", "type": "line", "params": [{"name": "region"}]},
            "external": {"family": "editor", "type": "selector"},
        },
        "datasets": {"sales": {"fields": {"region": {"title": "Region"}}}},
    }


@pytest.mark.parametrize(
    "change",
    [
        {"tab": "missing"},
        {"source": {"kind": "dataset", "dataset": "missing", "field": "Region"}},
        {"source": {"kind": "dataset", "dataset": "sales", "field": "Missing"}},
        {"source": {"kind": "manual"}},
        {"source": {"kind": "editor", "chart": "missing"}},
        {"source": {"kind": "editor", "chart": "trend"}},
        {"source": {"kind": "editor", "chart": "external"}, "group": "g"},
        {"source": {"kind": "unknown"}},
        {"recipients": "trend"},
        {"recipients": ["trend", "trend"]},
        {"recipients": ["external"]},
        {"recipients": ["unknown"]},
        {"control": {"element": "other"}},
        {"control": {"element": "select", "is_range": True}},
        {"control": {"element": "select", "unsupported": True}},
        {"control": {"element": "checkbox", "default_value": "true"}},
        {"control": {"element": "select", "title_placement": "right"}},
        {"control": {"element": "date", "is_range": True, "default_value": {"from": "2020"}}},
        {"control": {"element": "date", "default_value": {"from": "2020", "to": "2021"}}},
    ],
)
def test_invalid_selector_stops_locally(change):
    with pytest.raises(DataLensUtilsError):
        config._validate_selector("filter", selector(**change), **inputs())


def test_manual_selector_operation_is_not_dataset_comparison():
    value = selector(
        tab="ql",
        source={"kind": "manual", "param_name": "region"},
        recipients=["query"],
        control={"element": "select", "operation": "IN"},
    )
    with pytest.raises(DataLensUtilsError, match="comparison"):
        config._validate_selector("filter", value, **inputs())


@pytest.mark.parametrize(
    "position",
    [
        None,
        (0, 0, 1, 1),
        [0, 0, 1],
        [False, 0, 1, 1],
        [-1, 0, 1, 1],
        [0, -1, 1, 1],
        [0, 0, 0, 1],
        [0, 0, 1, 0],
        [35, 0, 2, 1],
    ],
)
def test_invalid_grid_position(position):
    with pytest.raises(DataLensUtilsError):
        config._validate_positions("tab", {"item": position})


def test_grid_overlap_is_explicitly_opt_in():
    config._validate_positions("tab", {"a": [0, 0, 1, 1], "b": [0, 0, 1, 1]}, allow_overlaps=True)
    config._validate_positions("tab", {"a": [1, 1, 1, 1], "b": [0, 0, 1, 1]})


def test_selector_file_missing_and_duplicate_keys(session_state):
    directory = session_state.paths.project_root / "configs/UI/selectors"
    path = directory / "invalid.json"
    for value in ({}, {"key": 1}, {"key": ""}):
        path.write_text(json.dumps(value))
        with pytest.raises(DataLensUtilsError, match="nonempty"):
            config.read_selector_definitions()
    original = next(
        p
        for p in directory.rglob("*.json")
        if p.name not in {"invalid.json", "selector_groups.json"}
    )
    path.write_text(original.read_text())
    with pytest.raises(DataLensUtilsError, match="Duplicate"):
        config.read_selector_definitions()


def files():
    return {
        "UI/layout.json": {
            "wizard": {"trend": [0, 5, 36, 10], "filters": [0, 0, 36, 3]},
            "ql": {"query": [0, 0, 36, 10]},
        },
        "DL objects/datasets.json": {"sales": {"fields": {"region": {"title": "Region"}}}},
        "UI/tabs.json": {"wizard": {}, "ql": {}},
        "UI/titles.json": {},
        "UI/texts.json": {},
        "UI/selectors/selector_groups.json": {"filters": {"tab": "wizard", "members": ["filter"]}},
        "UI/links/aliases.json": {},
        "UI/links/chart_params.json": {"query": {"region": ["North"]}},
        "UI/links/connections.json": {"filter": ["trend"]},
    }


@pytest.mark.parametrize(
    "problem",
    [
        "connections",
        "tabs",
        "titles",
        "duplicate group",
        "unknown group tab",
        "empty group",
        "membership",
        "unknown defaults",
        "undeclared defaults",
        "aliases",
        "duplicate item",
    ],
)
def test_combined_ui_rejects_inconsistent_declarations(problem):  # noqa: C901 - Scenario dispatch.
    values = copy.deepcopy(files())
    charts = {key: value for key, value in inputs()["charts"].items() if key != "external"}
    selectors = {"filter": selector(group="filters")}
    if problem == "connections":
        values["UI/links/connections.json"].clear()
    elif problem == "tabs":
        values["UI/layout.json"].pop("ql")
    elif problem == "titles":
        values["UI/titles.json"]["unknown"] = {}
    elif problem == "duplicate group":
        values["UI/selectors/selector_groups.json"]["other"] = {
            "tab": "wizard",
            "members": ["filter"],
        }
    elif problem == "unknown group tab":
        values["UI/selectors/selector_groups.json"]["filters"]["tab"] = "unknown"
    elif problem == "empty group":
        values["UI/selectors/selector_groups.json"]["filters"]["members"] = []
    elif problem == "membership":
        selectors["filter"].pop("group")
        values["UI/selectors/selector_groups.json"].clear()
    elif problem == "unknown defaults":
        values["UI/links/chart_params.json"]["unknown"] = {}
    elif problem == "undeclared defaults":
        values["UI/links/chart_params.json"]["query"] = {"missing": ["x"]}
    elif problem == "aliases":
        values["UI/links/aliases.json"]["wizard"] = [[{"dataset": "sales", "field": "Region"}]]
    else:
        values["UI/titles.json"]["ql"] = {"trend": {"text": "duplicate"}}
        values["UI/layout.json"]["ql"]["trend"] = [0, 15, 36, 1]
    if problem == "membership":
        selectors["filter"]["group"] = "undeclared"
    with patch.object(config, "read_config", side_effect=lambda name: values[name]), patch.object(
        config, "read_chart_definitions", return_value=charts
    ), patch.object(config, "read_selector_definitions", return_value=selectors), pytest.raises(
        DataLensUtilsError
    ):
        config.read_contents()
