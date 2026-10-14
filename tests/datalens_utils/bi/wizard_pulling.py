"""Public scoped pulls replace remote encodings and respect title ownership."""

import copy
import json

import pytest
from analytics_toolkit.datalens_utils.bi_verification import verify_resources
from analytics_toolkit.datalens_utils.errors import DataLensUtilsError
from analytics_toolkit.datalens_utils.recipe import load_registry
from analytics_toolkit.datalens_utils.resources.bi_store import BIResourceStore

from tests.datalens_utils._support.bi import write
from tests.datalens_utils._support.scoped_pull import CHART_FILE, DATASET_FILE, recovered_pull


@pytest.mark.parametrize(
    "change", ["remove_bindings", "change_kinds", "measure_color", "remove_palette", "remove_maps"]
)
def test_public_pull_replaces_complete_encoding_settings(bi_project, monkeypatch, change):
    settings = {
        "color_by_dimension": {"field": "category"},
        "shape_by_dimension": {"field": "category", "shapes_map": {"a": "circle"}},
        "palette": {"id": "defaultScheme"},
        "legend": {"mode": "hide"},
    }
    api, entities, checkpoint = recovered_pull(
        bi_project,
        monkeypatch,
        chart_overrides={
            "type": "scatter",
            "fields": {"x": ["Amount"], "y": ["Count"]},
            "settings": settings,
        },
    )
    root = bi_project.paths.project_root
    untouched = (root / DATASET_FILE).read_bytes()
    previous = json.loads(checkpoint.read_text())["resources"]
    visualization = entities["chart:trend"].data["visualization"]
    expected = copy.deepcopy(settings)
    if change == "remove_bindings":
        visualization["colors"] = {"items": []}
        visualization["shapes"] = {"items": []}
        expected = {"legend": settings["legend"]}
    elif change == "change_kinds":
        visualization["colors"] = {
            "items": [{"type": "PSEUDO"}],
            "settings": {"mountedColors": {"amount": "red"}},
        }
        visualization["shapes"] = {
            "items": [{"type": "PSEUDO"}],
            "settings": {"mountedShapes": {"amount": "circle"}},
        }
        expected = {
            "color_by_measure_name": {"colors_map": {"Amount": "red"}},
            "shape_by_measure_name": {"shapes_map": {"Amount": "circle"}},
            "legend": settings["legend"],
        }
    elif change == "measure_color":
        visualization["colors"] = {
            "items": [{"guid": "amount", "type": "MEASURE"}],
            "settings": {"gradientMode": "2-point", "gradientPalette": "blue", "reversed": True},
        }
        expected.pop("color_by_dimension")
        expected.pop("palette")
        expected["color_by_measure"] = {
            "field": "Amount",
            "mode": "2-point",
            "palette": "blue",
            "reversed": True,
        }
    elif change == "remove_palette":
        visualization["colors"]["settings"].pop("palette")
        expected.pop("palette")
    else:
        visualization["shapes"]["settings"].pop("mountedShapes")
        expected["shape_by_dimension"] = {"field": "category"}
    try:
        report = bi_project.pull(chart_keys=["trend"])
        assert report["pulled_resources"] == ["chart:trend"]
        pulled = json.loads((root / CHART_FILE).read_text())
        state = json.loads(checkpoint.read_text())["resources"]
        assert pulled["settings"] == expected
        assert state["chart:trend"]["baseline"]["definition"]["settings"] == expected
        assert (root / DATASET_FILE).read_bytes() == untouched
        assert state["dataset:sales"] == previous["dataset:sales"]
        recipe_bytes = (root / CHART_FILE).read_bytes()
        checkpoint_bytes = checkpoint.read_bytes()
        bi_project.pull(chart_keys=["trend"])
        assert (root / CHART_FILE).read_bytes() == recipe_bytes
        assert checkpoint.read_bytes() == checkpoint_bytes
        assert not api.writes
    finally:
        api.client.close()


@pytest.mark.parametrize("ownership", ["legacy", "managed", "absent"])
def test_public_pull_updates_internal_title_independently(bi_project, monkeypatch, ownership):
    overrides = {"title": "Widget", "show_title": False}
    if ownership != "legacy":
        overrides["chart_title"] = "Internal" if ownership == "managed" else None
    api, entities, checkpoint = recovered_pull(bi_project, monkeypatch, chart_overrides=overrides)
    root = bi_project.paths.project_root
    untouched = (root / DATASET_FILE).read_bytes()
    previous = json.loads(checkpoint.read_text())["resources"]
    entities["chart:trend"].data["visualization"].setdefault("chartSettings", {}).update(
        title="Remote internal", titleMode="hide"
    )
    try:
        bi_project.pull(chart_keys=["trend"])
        pulled = json.loads((root / CHART_FILE).read_text())
        state = json.loads(checkpoint.read_text())["resources"]
        baseline = state["chart:trend"]["baseline"]["definition"]
        for definition in (pulled, baseline):
            assert definition["title"] == ("Remote internal" if ownership == "legacy" else "Widget")
            if ownership == "legacy":
                assert "chart_title" not in definition
            else:
                assert definition["chart_title"] == "Remote internal"
            assert definition["show_title"] is False
        assert (root / DATASET_FILE).read_bytes() == untouched
        assert state["dataset:sales"] == previous["dataset:sales"]
        recipe_bytes = (root / CHART_FILE).read_bytes()
        checkpoint_bytes = checkpoint.read_bytes()
        bi_project.pull(chart_keys=["trend"])
        assert (root / CHART_FILE).read_bytes() == recipe_bytes
        assert checkpoint.read_bytes() == checkpoint_bytes
        assert not api.writes
    finally:
        api.client.close()


@pytest.mark.parametrize("problem", ["conflict", "unsupported"])
def test_public_encoding_pull_failure_leaves_files_unchanged(bi_project, monkeypatch, problem):
    api, entities, checkpoint = recovered_pull(
        bi_project,
        monkeypatch,
        chart_overrides={"settings": {"color_by_dimension": {"field": "category"}}},
    )
    root = bi_project.paths.project_root
    if problem == "conflict":
        definition = json.loads((root / CHART_FILE).read_text())
        definition["settings"]["color_by_dimension"] = {"field": "Local"}
        write(root, CHART_FILE, definition)
        entities["chart:trend"].data["visualization"]["colors"] = {
            "items": [{"guid": "amount", "type": "MEASURE"}]
        }
    else:
        entities["chart:trend"].data["visualization"]["colors"]["settings"]["unknown"] = True
    before = {path: (root / path).read_bytes() for path in (CHART_FILE, DATASET_FILE)}
    checkpoint_bytes = checkpoint.read_bytes()
    try:
        message = "changes conflict" if problem == "conflict" else "Unsupported colors settings"
        with pytest.raises(DataLensUtilsError, match=message):
            bi_project.pull(chart_keys=["trend"])
        assert {path: (root / path).read_bytes() for path in before} == before
        assert checkpoint.read_bytes() == checkpoint_bytes
        assert not api.writes
    finally:
        api.client.close()


@pytest.mark.parametrize("ownership", ["legacy", "managed", "absent"])
@pytest.mark.parametrize("missing_settings", [False, True])
def test_public_pull_preserves_removed_internal_title(
    bi_project, monkeypatch, ownership, missing_settings
):
    overrides = {"title": "Widget", "show_title": False}
    if ownership != "legacy":
        overrides["chart_title"] = "Internal" if ownership == "managed" else None
    api, entities, checkpoint = recovered_pull(bi_project, monkeypatch, chart_overrides=overrides)
    root = bi_project.paths.project_root
    untouched = (root / DATASET_FILE).read_bytes()
    previous = json.loads(checkpoint.read_text())["resources"]
    visualization = entities["chart:trend"].data["visualization"]
    if missing_settings:
        visualization.pop("chartSettings", None)
    else:
        visualization.setdefault("chartSettings", {}).pop("title", None)
    try:
        report = bi_project.pull(chart_keys=["trend"])
        assert report["pulled_resources"] == ["chart:trend"]
        pulled = json.loads((root / CHART_FILE).read_text())
        state = json.loads(checkpoint.read_text())["resources"]
        baseline = state["chart:trend"]["baseline"]["definition"]
        for definition in (pulled, baseline):
            assert definition["title"] == "Widget"
            assert definition["show_title"] is False
            if ownership == "legacy":
                assert "chart_title" not in definition
            else:
                assert "chart_title" in definition
                assert definition["chart_title"] is None
        assert (root / DATASET_FILE).read_bytes() == untouched
        assert state["dataset:sales"] == previous["dataset:sales"]
        recipe_bytes = (root / CHART_FILE).read_bytes()
        checkpoint_bytes = checkpoint.read_bytes()
        if ownership != "legacy":
            # Exercise persisted metadata verification with the published SDK snapshot.
            with bi_project.session():
                registry = load_registry()
                store = BIResourceStore(registry, readonly=True)
                verified = verify_resources(api.client, store, registry, {"chart:trend"}, entities)
            assert verified["verified_resources"] == ["chart:trend"]
            assert verified["mismatches"] == []
        bi_project.pull(chart_keys=["trend"])
        assert (root / CHART_FILE).read_bytes() == recipe_bytes
        assert checkpoint.read_bytes() == checkpoint_bytes
        assert (root / DATASET_FILE).read_bytes() == untouched
        assert not api.writes
    finally:
        api.client.close()


def test_public_pull_conflicting_title_removal_keeps_files_and_checkpoint(bi_project, monkeypatch):
    api, entities, checkpoint = recovered_pull(
        bi_project, monkeypatch, chart_overrides={"title": "Widget", "chart_title": "Internal"}
    )
    root = bi_project.paths.project_root
    definition = json.loads((root / CHART_FILE).read_text())
    definition["chart_title"] = "Local internal"
    write(root, CHART_FILE, definition)
    entities["chart:trend"].data["visualization"]["chartSettings"].pop("title")
    before = {path: (root / path).read_bytes() for path in (CHART_FILE, DATASET_FILE)}
    checkpoint_bytes = checkpoint.read_bytes()
    try:
        with pytest.raises(DataLensUtilsError, match="changes conflict"):
            bi_project.pull(chart_keys=["trend"])
        assert {path: (root / path).read_bytes() for path in before} == before
        assert checkpoint.read_bytes() == checkpoint_bytes
        assert not api.writes
    finally:
        api.client.close()
