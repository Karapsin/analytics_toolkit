"""Strict version-2 contracts fail locally before runtime or cloud access."""

import copy

import pytest
from analytics_toolkit.datalens_utils.errors import DataLensConfigurationError
from analytics_toolkit.datalens_utils.recipe import Resource, ResourceRegistry, load_registry

from tests.datalens_utils._support.bi import write


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"mode": "unknown"}, "mode must be"),
        ({"connector": ""}, "requires a connector"),
        ({"name": "  "}, "nonempty name"),
        ({"parameters": []}, "parameters must be an object"),
        ({"secrets": []}, "secrets must be an object"),
        ({"parameters": {"host": "remote"}}, "read-only references"),
        ({"mode": "managed", "secrets": {"password": "BI_PASSWORD"}}, "credentials_revision"),
        (
            {"mode": "managed", "credentials_revision": "v1", "secrets": {"password": 1}},
            "environment variable",
        ),
        ({"unknown": True}, "Unknown connection"),
    ],
)
def test_invalid_connection_contract_has_no_runtime_side_effects(bi_project, change, message):
    root = bi_project.paths.project_root
    value = {**load_registry().resources["connection:ch"].definition, **change}
    write(root, "configs/DL objects/connections.json", {"ch": value})
    with pytest.raises(DataLensConfigurationError, match=message):
        bi_project.validate()
    assert not bi_project.paths.runtime_root.exists()


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"sources": {}}, "requires named sources"),
        ({"sources": []}, "sources must be an object"),
        ({"sources": {"source": {"connection": "ch"}}}, "connection and factory"),
        ({"fields": {"x": {"source": "unknown", "column": "x"}}}, "source and column"),
        (
            {"fields": {"x": {"source": "source", "column": "x", "kind": "MEASURE"}}},
            "kind must match",
        ),
        ({"fields": {"Amount": {"source": "source", "column": "x"}}}, "must be unique"),
        (
            {"fields": {"x": {"source": "source", "column": "x", "guid": "amount"}}},
            "must be unique",
        ),
        ({"relations": {"r": {"left": "unknown", "right": "source"}}}, "unknown source"),
        (
            {"relations": {"r": {"left": "source", "right": "source", "type": "left"}}},
            "distinct sources",
        ),
        (
            {"rls": {"r": {"field": "category", "subject_type": "robot", "subject_id": "r"}}},
            "subject identity",
        ),
        ({"cache_invalidation": {"mode": "unknown"}}, "cache mode"),
        ({"cache_invalidation": {"mode": "sql"}}, "requires sql_file"),
        (
            {
                "cache_invalidation": {
                    "mode": "formula",
                    "field": {"guid": "c", "type": "float", "formula": {"formula": "1"}},
                }
            },
            "both representations",
        ),
        (
            {"cache_invalidation": {"mode": "off", "filters": [{"field": "category"}]}},
            "stable keys",
        ),
        ({"default_filters": [{"field": "category", "invented": True}]}, "Unknown dataset"),
    ],
)
def test_invalid_dataset_contract_is_rejected_before_sdk_requests(bi_project, change, message):
    root = bi_project.paths.project_root
    value = {**copy.deepcopy(load_registry().resources["dataset:sales"].definition), **change}
    write(root, "configs/DL objects/datasets.json", {"sales": value})
    with pytest.raises(DataLensConfigurationError, match=message):
        bi_project.validate()
    assert not bi_project.paths.runtime_root.exists()


def test_named_dependency_errors_and_selection_are_explicit():
    resource = Resource("dataset:sales", "dataset", {}, {}, ("connection:ch",))
    with pytest.raises(DataLensConfigurationError, match="missing resources"):
        ResourceRegistry({resource.key: resource})
    cycle = Resource("connection:ch", "connection", {}, {}, (resource.key,))
    with pytest.raises(DataLensConfigurationError, match="cycle"):
        ResourceRegistry({resource.key: resource, cycle.key: cycle})
    registry = ResourceRegistry({"connection:ch": Resource("connection:ch", "connection", {}, {})})
    with pytest.raises(DataLensConfigurationError, match="cannot be empty"):
        registry.selection([])
    with pytest.raises(DataLensConfigurationError, match="Unknown resource keys"):
        registry.selection(["dataset:missing"])
    assert registry.selection(None) == {"connection:ch"}


def test_object_manifests_require_mapping(bi_project):
    write(bi_project.paths.project_root, "configs/DL objects/html_pages.json", [])
    with pytest.raises(DataLensConfigurationError, match="definitions must be an object"):
        bi_project.validate()
