"""Scoped facade pulls retain recipe GUIDs after backend inference."""

import copy
import json
from dataclasses import replace

import pytest
from analytics_toolkit.datalens_utils.bi_engine import local_datasets
from analytics_toolkit.datalens_utils.bi_pull_identity import normalize_dataset
from analytics_toolkit.datalens_utils.dataset_identity import resolved_guid
from analytics_toolkit.datalens_utils.editing.state import fingerprint
from analytics_toolkit.datalens_utils.errors import DataLensUtilsError
from analytics_toolkit.datalens_utils.recipe import load_registry
from analytics_toolkit.datalens_utils.resources.bi_store import safe_metadata

from tests.datalens_utils._support.bi import dataset, write
from tests.datalens_utils._support.scoped_pull import CHART_FILE, DATASET_FILE, recovered_pull


@pytest.mark.parametrize("scope", ["dataset", "chart"])
def test_public_scoped_pull_after_inference_preserves_unselected_resources(  # noqa: PLR0915
    bi_project, monkeypatch, scope
):
    api, entities, checkpoint = recovered_pull(bi_project, monkeypatch)
    root = bi_project.paths.project_root
    selected_file = DATASET_FILE if scope == "dataset" else CHART_FILE
    unselected_file = CHART_FILE if scope == "dataset" else DATASET_FILE
    selected_key = "dataset:sales" if scope == "dataset" else "chart:trend"
    unselected_key = "chart:trend" if scope == "dataset" else "dataset:sales"
    if scope == "dataset":
        values = json.loads((root / DATASET_FILE).read_text())
        values["sales"]["fields"]["category"]["hidden"] = True
        write(root, DATASET_FILE, values)
        cloud = entities["dataset:sales"]
        entities["dataset:sales"] = replace(
            cloud,
            name="Remote sales",
            result_schema=(cloud.result_schema[0], {**cloud.result_schema[1], "formula": "SUM(2)"}),
        )
        options = {"dataset_keys": ["sales"]}
    else:
        values = json.loads((root / CHART_FILE).read_text())
        values["description"] = "Independent local note"
        write(root, CHART_FILE, values)
        entities["chart:trend"] = replace(entities["chart:trend"], name="Remote trend")
        options = {"chart_keys": ["trend"]}
    untouched = (root / unselected_file).read_bytes()
    previous = json.loads(checkpoint.read_text())["resources"]
    try:
        report = bi_project.pull(**options)
        assert report["pulled_resources"] == [selected_key]
        pulled = json.loads((root / selected_file).read_text())
        if scope == "dataset":
            assert pulled["sales"]["fields"]["category"]["guid"] == "category"
            assert pulled["sales"]["fields"]["category"]["hidden"]
            assert pulled["sales"]["calculations"]["Amount"]["formula"] == "SUM(2)"
            assert pulled["sales"]["name"] == "Remote sales"
        else:
            assert pulled["name"] == "Remote trend"
            assert pulled["description"] == "Independent local note"
            assert pulled["aggregated_measures"]["Count"]["field"] == "category"
            assert pulled["aggregated_measures"]["Count"]["guid"] == "count"
            assert pulled["hierarchies"]["Group"] == {
                "guid": "group",
                "fields": ["category", "local"],
            }
            assert pulled["local_fields"][0]["guid"] == "local"
        assert (root / unselected_file).read_bytes() == untouched
        state = json.loads(checkpoint.read_text())["resources"]
        assert state[unselected_key] == previous[unselected_key]
        assert state["dataset:sales"]["field_guids"]["category"] == "inferred-category"
        baseline = state[selected_key]["baseline"]["definition"]
        if scope == "dataset":
            assert baseline["fields"]["category"]["guid"] == "category"
        else:
            assert baseline["aggregated_measures"]["Count"]["field"] == "category"
        recipe_bytes = (root / selected_file).read_bytes()
        checkpoint_bytes = checkpoint.read_bytes()
        bi_project.pull(**options)
        assert (root / selected_file).read_bytes() == recipe_bytes
        assert checkpoint.read_bytes() == checkpoint_bytes
        assert not api.writes
        # Both the synthetic validator and cloud builders can use the same recipe.
        assert resolved_guid(local_datasets(load_registry())["sales"], "category") == "category"
        assert resolved_guid(entities["dataset:sales"], "category") == "inferred-category"
    finally:
        api.client.close()


@pytest.mark.parametrize("problem", ["conflict", "duplicate_column", "foreign_checkpoint"])
def test_public_pull_failure_keeps_recipe_and_checkpoint(bi_project, monkeypatch, problem):
    api, entities, checkpoint = recovered_pull(bi_project, monkeypatch)
    root = bi_project.paths.project_root
    if problem == "conflict":
        value = json.loads((root / CHART_FILE).read_text())
        value["name"] = "Local name"
        write(root, CHART_FILE, value)
        entities["chart:trend"] = replace(entities["chart:trend"], name="Remote name")
    elif problem == "duplicate_column":
        cloud = entities["dataset:sales"]
        entities["dataset:sales"] = replace(
            cloud,
            result_schema=(*cloud.result_schema, {**cloud.result_schema[0], "guid": "duplicate"}),
        )
    else:
        state = json.loads(checkpoint.read_text())
        state["resources"]["dataset:sales"]["id"] = "foreign"
        write(checkpoint.parent, checkpoint.name, state)
    before = {path: (root / path).read_bytes() for path in (DATASET_FILE, CHART_FILE)}
    checkpoint_bytes = checkpoint.read_bytes()
    try:
        message = {
            "conflict": "changes conflict",
            "duplicate_column": "unambiguously",
            "foreign_checkpoint": "retained GUID",
        }[problem]
        with pytest.raises(DataLensUtilsError, match=message):
            bi_project.pull(chart_keys=["trend"])
        assert {path: (root / path).read_bytes() for path in before} == before
        assert checkpoint.read_bytes() == checkpoint_bytes
        assert not api.writes
    finally:
        api.client.close()


@pytest.mark.parametrize(
    ("identifier", "guids", "expected"),
    [
        ("sales", ["category", "inferred-category"], "category"),
        ("dataset:sales", ["category"], "category"),
        ("sales", ["inferred-category"], "inferred-category"),
        ("sales", ["missing"], "category"),
        ("foreign", ["inferred-category"], "category"),
    ],
)
def test_guid_resolution_requires_present_field_and_matching_checkpoint(
    bi_project, identifier, guids, expected
):
    write(
        bi_project.paths.runtime_root,
        "resources.json",
        {
            "resources": {
                "dataset:sales": {
                    "id": "sales",
                    "recipe_field_guids": {"category": "category"},
                    "field_guids": {"category": "inferred-category"},
                },
            },
        },
    )
    original = dataset()
    cloud = replace(
        original,
        id=identifier,
        result_schema=tuple({**original.result_schema[0], "guid": guid} for guid in guids),
    )
    assert resolved_guid(cloud, "category") == expected


def test_dataset_normalization_restores_cache_guids_without_rewriting_authored_formula():
    incoming = {
        "fields": {"a": {"guid": "inferred"}},
        "cache_invalidation": {
            "field": {
                "formula": {
                    "formula": "MAX([Title])",
                    "guid_formula": "MAX([inferred]) + [unmanaged]",
                },
            },
        },
    }
    normalize_dataset(incoming, {"fields": {"a": {"guid": "recipe"}}})
    assert incoming["fields"]["a"]["guid"] == "recipe"
    assert incoming["cache_invalidation"]["field"]["formula"] == {
        "formula": "MAX([Title])",
        "guid_formula": "MAX([recipe]) + [unmanaged]",
    }
    ambiguous = {"fields": {"a": {"guid": "inferred"}, "b": {"guid": "inferred"}}}
    with pytest.raises(DataLensUtilsError, match="ambiguous recipe"):
        normalize_dataset(
            copy.deepcopy(ambiguous),
            {"fields": {"a": {"guid": "first"}, "b": {"guid": "second"}}},
        )


@pytest.mark.parametrize("scope", ["dataset", "chart"])
def test_public_clean_local_pull_retains_complete_baseline(bi_project, monkeypatch, scope):
    source_sql = "SELECT category FROM sales"
    api, entities, checkpoint = recovered_pull(bi_project, monkeypatch, source_sql=source_sql)
    root = bi_project.paths.project_root
    selected_key = "dataset:sales" if scope == "dataset" else "chart:trend"
    unselected_key = "chart:trend" if scope == "dataset" else "dataset:sales"
    selected_file = DATASET_FILE if scope == "dataset" else CHART_FILE
    unselected_file = CHART_FILE if scope == "dataset" else DATASET_FILE
    options = {"dataset_keys": ["sales"]} if scope == "dataset" else {"chart_keys": ["trend"]}
    previous = json.loads(checkpoint.read_text())["resources"]
    assert load_registry().resources[selected_key].files == previous[selected_key]["baseline"]
    entities[selected_key] = replace(entities[selected_key], name="Remote name")
    untouched = (root / unselected_file).read_bytes()
    try:
        report = bi_project.pull(**options)
        assert report["pulled_resources"] == [selected_key]
        state = json.loads(checkpoint.read_text())["resources"]
        baseline = state[selected_key]["baseline"]
        assert baseline == load_registry().resources[selected_key].files
        assert baseline["definition"]["name"] == "Remote name"
        assert state[selected_key]["fingerprint"] == fingerprint(baseline)
        assert state[selected_key]["metadata"] == safe_metadata(entities[selected_key], scope)
        assert state[unselected_key] == previous[unselected_key]
        assert (root / unselected_file).read_bytes() == untouched
        assert state["dataset:sales"]["field_guids"]["category"] == "inferred-category"
        assert state["dataset:sales"]["baseline"]["assets/source.sql"] == source_sql
        assert (root / "assets/source.sql").read_text() == source_sql
        if scope == "dataset":
            assert baseline["definition"]["fields"]["category"]["guid"] == "category"
        else:
            assert baseline["definition"]["aggregated_measures"]["Count"]["field"] == "category"
            assert baseline["definition"]["local_fields"][0]["guid"] == "local"
            assert baseline["definition"]["hierarchies"]["Group"]["fields"] == ["category", "local"]
        before = {path: (root / path).read_bytes() for path in (selected_file, unselected_file)}
        checkpoint_bytes = checkpoint.read_bytes()
        bi_project.pull(**options)
        assert {path: (root / path).read_bytes() for path in before} == before
        assert checkpoint.read_bytes() == checkpoint_bytes
        assert (root / "assets/source.sql").read_text() == source_sql
        assert not api.writes
    finally:
        api.client.close()
