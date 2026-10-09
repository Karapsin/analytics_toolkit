"""Full-dashboard and selected-tab import remain local, reviewable operations."""

import json
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest
from analytics_toolkit.datalens_utils.bi_import import _merge_mapping
from analytics_toolkit.datalens_utils.recipe_io import replace_files
from datalens_sdk import Dashboard


def source_dashboard():
    return Dashboard(
        id="source",
        saved_id="r",
        published_id="r",
        data={
            "tabs": [
                {
                    "id": "one",
                    "title": "One",
                    "items": [
                        {
                            "id": "heading-one",
                            "type": "title",
                            "data": {"text": "First", "size": "m"},
                        }
                    ],
                    "layout": [{"i": "heading-one", "x": 0, "y": 0, "w": 12, "h": 2}],
                },
                {
                    "id": "two",
                    "title": "Two",
                    "items": [{"id": "heading-two", "type": "text", "data": {"text": "Second"}}],
                    "layout": [{"i": "heading-two", "x": 0, "y": 0, "w": 12, "h": 2}],
                },
            ]
        },
    )


def test_full_dashboard_preview_and_merge(bi_project):
    client = SimpleNamespace(get=SimpleNamespace(dashboard=lambda **kwargs: source_dashboard()))
    bi_project.client_factory = lambda _: nullcontext(client)
    root = bi_project.paths.project_root
    before = {
        path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()
    }
    preview = bi_project.import_dashboard(dashboard_id="source")
    assert not preview["written"]
    assert not preview["blockers"]
    assert set(preview["proposed_files"]["configs/UI/tabs.json"]) == {"main", "one", "two"}
    assert before == {
        path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()
    }
    assert not bi_project.paths.runtime_root.exists()
    result = bi_project.import_dashboard(dashboard_id="source", dry_run=False)
    assert result["written"]
    assert set(json.loads((root / "configs/UI/tabs.json").read_text())) == {"main", "one", "two"}
    assert not bi_project.paths.runtime_root.exists()
    assert bi_project.validate()["coverage"]["schema_version"] == 2


def test_tab_import_selects_only_requested_tab(bi_project):
    bi_project.client_factory = lambda _: nullcontext(
        SimpleNamespace(get=SimpleNamespace(dashboard=lambda **kwargs: source_dashboard()))
    )
    report = bi_project.import_tab(dashboard_id="source", tab_id="two")
    assert not report["blockers"]
    assert set(report["proposed_files"]["configs/UI/tabs.json"]) == {"main", "two"}
    with pytest.raises(Exception, match="absent"):
        bi_project.import_tab(dashboard_id="source", tab_id="missing")


def test_collision_keeps_tree_unchanged(bi_project):
    source = source_dashboard()
    source.data["tabs"][0]["id"] = "main"
    bi_project.client_factory = lambda _: nullcontext(
        SimpleNamespace(get=SimpleNamespace(dashboard=lambda **kwargs: source))
    )
    path = bi_project.paths.project_root / "configs/UI/tabs.json"
    before = path.read_bytes()
    result = bi_project.import_dashboard(dashboard_id="source", dry_run=False)
    assert result["blockers"]
    assert not result["written"]
    assert path.read_bytes() == before
    assert _merge_mapping({"a": 1}, {"b": 2}, "file") == {"a": 1, "b": 2}


def test_unsupported_item_has_fidelity_blocker(bi_project):
    source = source_dashboard()
    source.data["tabs"][0]["items"][0]["type"] = "neuro_widget"
    bi_project.client_factory = lambda _: nullcontext(
        SimpleNamespace(get=SimpleNamespace(dashboard=lambda **kwargs: source))
    )
    report = bi_project.import_dashboard(dashboard_id="source", dry_run=False)
    assert not report["written"]
    assert any("Unsupported dashboard item" in item["reason"] for item in report["blockers"])


def test_failed_local_replace_rolls_back(bi_project, monkeypatch):
    path = bi_project.paths.project_root / "configs/UI/tabs.json"
    before = path.read_bytes()
    original = Path.replace

    def fail_second(source, target):
        if target.name == "widgets.json":
            message = "write failed"
            raise OSError(message)
        return original(source, target)

    monkeypatch.setattr(Path, "replace", fail_second)
    with pytest.raises(OSError, match="write failed"):
        replace_files(
            {"configs/UI/tabs.json": {"new": {"title": "New"}}, "configs/UI/widgets.json": {}}
        )
    assert path.read_bytes() == before
    assert not (bi_project.paths.project_root / "configs/UI/widgets.json").exists()
