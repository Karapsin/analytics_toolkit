"""Three-way recipe pulls, stable rule/filter ownership and atomic local writes."""

import copy
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from analytics_toolkit.datalens_utils.bi_pull import extract_dataset, pull_resources
from analytics_toolkit.datalens_utils.errors import DataLensUtilsError
from analytics_toolkit.datalens_utils.recipe import load_registry
from analytics_toolkit.datalens_utils.resources.bi_store import BIResourceStore
from datalens_sdk import DataLensClientYC, NoAuthProvider

from tests.datalens_utils._support.bi import dataset, write


def changed_formula(formula="SUM(2)"):
    original = dataset()
    return replace(
        original,
        saved_id="remote",
        published_id="remote",
        result_schema=(
            original.result_schema[0],
            {**original.result_schema[1], "formula": formula},
        ),
    )


def test_dataset_pull_merges_independent_local_and_remote_changes(bi_project):
    registry = load_registry()
    store = BIResourceStore(registry)
    store.remember("dataset:sales", dataset())
    definition = copy.deepcopy(registry.resources["dataset:sales"].definition)
    definition["fields"]["category"]["hidden"] = True
    write(bi_project.paths.project_root, "configs/DL objects/datasets.json", {"sales": definition})
    registry = load_registry()
    store = BIResourceStore(registry, readonly=True)
    with DataLensClientYC(auth=NoAuthProvider()) as client:
        result = pull_resources(
            client,
            store,
            registry,
            {"dataset:sales"},
            {"dataset:sales": changed_formula()},
            branch="published",
        )
    assert result["pulled_resources"] == ["dataset:sales"]
    pulled = json.loads(
        (bi_project.paths.project_root / "configs/DL objects/datasets.json").read_text()
    )["sales"]
    assert pulled["fields"]["category"]["hidden"]
    assert pulled["calculations"]["Amount"]["formula"] == "SUM(2)"
    assert pulled["fields"]["category"]["guid"] == "category"
    assert store.state["resources"]["dataset:sales"]["metadata"]["published_id"] == "remote"
    assert (
        store.state["resources"]["dataset:sales"]["baseline"]["definition"]["calculations"][
            "Amount"
        ]["formula"]
        == "SUM(2)"
    )


@pytest.mark.parametrize("problem", ["missing_id", "baseline", "conflict"])
def test_pull_conflicts_leave_recipe_and_checkpoint_unchanged(bi_project, problem):
    registry = load_registry()
    store = BIResourceStore(registry)
    store.remember("dataset:sales", dataset())
    if problem == "baseline":
        store.state["resources"]["dataset:sales"].pop("baseline")
    if problem == "conflict":
        definition = copy.deepcopy(registry.resources["dataset:sales"].definition)
        definition["calculations"]["Amount"]["formula"] = "SUM(3)"
        write(
            bi_project.paths.project_root, "configs/DL objects/datasets.json", {"sales": definition}
        )
        registry = load_registry()
    before = (bi_project.paths.project_root / "configs/DL objects/datasets.json").read_bytes()
    checkpoint = store.path.read_bytes()
    with DataLensClientYC(auth=NoAuthProvider()) as client, pytest.raises(DataLensUtilsError):
        pull_resources(
            client,
            store,
            registry,
            {"dataset:sales"},
            {} if problem == "missing_id" else {"dataset:sales": changed_formula()},
            branch="published",
        )
    assert (
        bi_project.paths.project_root / "configs/DL objects/datasets.json"
    ).read_bytes() == before
    assert store.path.read_bytes() == checkpoint


def test_pull_filter_and_rls_edits_use_stable_ownership(bi_project):
    definition = copy.deepcopy(load_registry().resources["dataset:sales"].definition)
    definition["default_filters"] = [{"field": "category", "operator": "IN", "values": ["old"]}]
    definition["rls"] = {
        "access": {"field": "category", "subject_id": "user", "allowed_value": "old"},
        "removed": {"field": "category", "subject_id": "gone", "allowed_value": "gone"},
    }
    original = dataset()
    value = replace(
        original,
        obligatory_filters=(
            {
                "id": "owned",
                "field_guid": "category",
                "default_filters": [{"column": "category", "operation": "IN", "values": ["new"]}],
            },
            {
                "id": "unmanaged",
                "field_guid": "category",
                "default_filters": [
                    {"column": "category", "operation": "EQ", "values": ["unmanaged"]}
                ],
            },
        ),
        rls2={
            "category": [
                {
                    "field_guid": "category",
                    "subject": {"subject_id": "user", "subject_type": "user"},
                    "allowed_value": "new",
                    "pattern_type": "value",
                }
            ]
        },
    )
    pulled = extract_dataset(
        value,
        SimpleNamespace(definition=definition),
        {"default_filters_owned": {"category": "owned"}},
    )
    assert pulled["default_filters"] == [{"field": "category", "operator": "IN", "values": ["new"]}]
    assert set(pulled["rls"]) == {"access"}
    assert pulled["rls"]["access"]["allowed_value"] == "new"
    with pytest.raises(DataLensUtilsError, match="default filter"):
        extract_dataset(value, SimpleNamespace(definition=definition), {})


def test_unrepresentable_rls_rule_count_blocks_extraction(bi_project):
    definition = copy.deepcopy(load_registry().resources["dataset:sales"].definition)
    definition["rls"] = {
        "access": {"field": "category", "subject_id": "user", "allowed_value": "old"}
    }
    rule = {
        "field_guid": "category",
        "subject": {"subject_id": "user", "subject_type": "user"},
        "allowed_value": "new",
        "pattern_type": "value",
    }
    value = replace(dataset(), rls2={"category": [rule, rule]})
    with pytest.raises(DataLensUtilsError, match="rule count"):
        extract_dataset(value, SimpleNamespace(definition=definition), {})


@pytest.mark.parametrize("mode", ["off", "formula", "sql"])
def test_cache_pull_preserves_typed_mode_and_asset_identity(bi_project, mode):
    definition = copy.deepcopy(load_registry().resources["dataset:sales"].definition)
    definition["cache_invalidation"] = {"mode": "sql", "sql_file": "assets/cache.sql"}
    remote = {"mode": mode, "filters": []}
    if mode == "formula":
        remote["field"] = {
            "guid": "cache",
            "type": "MEASURE",
            "calc_spec": {"formula": "SUM(1)", "guid_formula": "SUM(1)"},
        }
    if mode == "sql":
        remote["sql"] = "SELECT MAX(id)"
    value = replace(dataset(), raw={"dataset": {"cache_invalidation_source": remote}})
    pulled = extract_dataset(value, SimpleNamespace(definition=definition), {})
    assert pulled["cache_invalidation"]["mode"] == mode
    if mode == "sql":
        assert pulled["cache_invalidation"]["sql_file"] == "assets/cache.sql"
    else:
        assert "sql_file" not in pulled["cache_invalidation"]
    if mode == "formula":
        assert pulled["cache_invalidation"]["field"]["formula"]["guid_formula"] == "SUM(1)"
