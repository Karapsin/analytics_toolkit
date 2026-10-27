"""Authored-source analysis and local publication reject silent loss."""

import copy
from dataclasses import replace
from types import SimpleNamespace

import pytest
from analytics_toolkit.datalens_utils.errors import DataLensConfigurationError
from analytics_toolkit.datalens_utils.import_fidelity import wizard_source_issues
from analytics_toolkit.datalens_utils.import_source import (
    model_record,
    restore_model,
    source_assets,
    wizard_bindings,
)
from analytics_toolkit.datalens_utils.recipe import load_registry
from analytics_toolkit.datalens_utils.recipe_io import publish_project
from analytics_toolkit.datalens_utils.session import current_session
from datalens_sdk import Connection, EntryLocation

from tests.datalens_utils._support.captured_import import imported_project, run_import


@pytest.mark.parametrize("workbook", ["book", None])
def test_connection_cache_retains_location_without_credentials(workbook):
    entity = Connection(
        id="retained",
        name="Connection",
        type="clickhouse",
        installation="yacloud",
        location=EntryLocation.workbook(workbook) if workbook else None,
        raw={"password": "not-cacheable", "saved_id": "connection-r", "rev_id": "connection-r"},
    )
    record = model_record(entity)
    assert "not-cacheable" not in repr(record)
    restored = restore_model(record)
    assert restored.id == entity.id
    assert restored.workbook_id == workbook
    assert restored.saved_id == "connection-r"
    assert restored.rev_id == "connection-r"


def test_local_formula_binding_uses_explicit_dataset_identity():
    entity = SimpleNamespace(
        dataset_ids=["unused", "used"],
        data={
            "sources": {
                "updates": [
                    {"field": {"guid": "calculation", "datasetId": "used", "local": True}},
                    {},
                ]
            },
            "visualization": {"y": [{"guid": "calculation", "datasetId": "used"}]},
        },
    )
    datasets = {
        name: SimpleNamespace(id=name, fields=[]) for name in ("unused", "used", "unattached")
    }
    result = wizard_bindings(entity, datasets)
    assert result["referenced_dataset_ids"] == ["used"]
    assert result["unused_dataset_ids"] == ["unused"]
    assert not result["ambiguous"]


@pytest.mark.parametrize("category", ["ql", "editor"])
def test_nontext_source_values_do_not_create_fabricated_assets(category):
    assert (
        source_assets(
            {
                "kind": "chart",
                "category": category,
                "metadata": {"id": "source"},
                "data": {"config": {}, "queryValue": None},
            }
        )
        == {}
    )


def test_fidelity_detects_chart_settings_and_slots_missing_from_recipe(import_source, tmp_path):
    run_import(import_source, tmp_path, dry_run=False)
    data = copy.deepcopy(import_source.chart.data)
    data["visualization"]["chartSettings"] = {"futureSetting": "authored"}
    data["visualization"]["futureSlot"] = {
        "items": [{"guid": next(iter(import_source.dataset.fields)).guid}]
    }
    chart = replace(import_source.chart, data=data)
    with imported_project(import_source, tmp_path).session():
        registry = load_registry()
        definition = next(
            item.definition for item in registry.resources.values() if item.kind == "chart"
        )
        datasets = {
            key.split(":", 1)[1]: import_source.dataset
            for key, item in registry.resources.items()
            if item.kind == "dataset"
        }
        issues = wizard_source_issues(import_source.client, chart, datasets, definition)
    assert "unrepresented chart setting futureSetting" in issues
    assert "unrepresented slot futureSlot" in issues


@pytest.mark.parametrize("failure", ["path-escape", "unserializable-asset", "destination-race"])
def test_publication_failure_keeps_destination_and_cleans_staging(bi_project, tmp_path, failure):
    root = tmp_path / "project"
    root.mkdir()
    files = {"configs/good.json": {"value": "retained"}}
    expected = (DataLensConfigurationError,)
    if failure == "path-escape":
        files["../escape.txt"] = "must not escape"
    elif failure == "unserializable-asset":
        files["assets/invalid.js"] = object()
        expected = (AttributeError,)
    else:
        (root / "concurrent.txt").write_text("keep")
    with current_session().staged(root), pytest.raises(expected):
        publish_project(files)
    assert root.is_dir()
    assert not (root / "configs/good.json").exists()
    assert not (tmp_path / "escape.txt").exists()
    assert not list(tmp_path.glob(".datalens-import-*"))
    if failure == "destination-race":
        assert (root / "concurrent.txt").read_text() == "keep"
