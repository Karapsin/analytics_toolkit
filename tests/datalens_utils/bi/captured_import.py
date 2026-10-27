"""Fresh-project source capture through public SDK reads and offline transport."""

import copy
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from analytics_toolkit.cli import main
from analytics_toolkit.datalens_utils import (
    DataLensAuthConfig,
    import_cli,
)
from analytics_toolkit.datalens_utils.bi_import import chart_dependencies
from analytics_toolkit.datalens_utils.import_api import dashboard_id
from analytics_toolkit.datalens_utils.import_manifest import guard_write
from analytics_toolkit.datalens_utils.import_refresh import refresh_source
from analytics_toolkit.datalens_utils.import_source import (
    model_record,
    restore_model,
    source_assets,
    wizard_bindings,
)
from analytics_toolkit.datalens_utils.recipe import load_registry
from datalens_sdk import APIErrorContext, EntryLocation, ForbiddenError

from tests.datalens_utils._support.captured_import import imported_project, run_import


def test_new_project_preview_write_and_offline_integrity(import_source, tmp_path):
    original = copy.deepcopy(import_source.api.documents)
    before_writes = list(import_source.api.writes)
    preview = run_import(import_source, tmp_path)
    assert preview["capture_complete"]
    assert not preview["written"]
    assert not (tmp_path / "project").exists()
    assert preview["coverage"]["captured"] == 4
    written = run_import(import_source, tmp_path, dry_run=False, resume=True)
    assert written["written"]
    assert (
        repr(str((tmp_path / "runtime").resolve()))
        in (tmp_path / "project/dashboard.py").read_text()
    )
    assert written["cache_hits"] >= 3
    assert import_source.api.documents == original
    assert import_source.api.writes == before_writes
    assert all(
        kwargs.get("branch") == "published"
        for kind, kwargs in import_source.reads
        if kind != "connection"
    )
    manifest = json.loads((tmp_path / "project/configs/import.json").read_text())
    project = imported_project(import_source, tmp_path)
    assert project.validate()["coverage"]["resource_count"] == 4
    entry = next(value for value in manifest["resources"].values() if value["kind"] == "chart")
    path = tmp_path / "project" / entry["source_file"]
    path.write_text("{}")
    with pytest.raises(Exception, match="integrity"):
        project.validate()


def test_readable_unsupported_chart_is_retained(import_source, tmp_path):
    chart = import_source.chart
    data = copy.deepcopy(chart.data)
    data["visualization"]["type"] = "future-visualization"
    snapshot = copy.deepcopy(chart.response_snapshot)
    snapshot["entry"]["data"] = data
    import_source.entities[chart.id] = replace(chart, data=data, response_snapshot=snapshot)
    report = run_import(import_source, tmp_path, dry_run=False)
    entry = next(item for item in report["resources"].values() if item["id"] == chart.id)
    assert report["capture_complete"]
    assert not entry["editable"]
    assert entry["restrictions"]
    assert json.loads((tmp_path / "project" / entry["source_file"]).read_text())["data"] == data


def test_published_capture_ignores_newer_saved_draft(import_source, tmp_path):
    dashboard = replace(import_source.dashboard, saved_id="newer-draft")
    import_source.entities[dashboard.id] = dashboard
    report = run_import(import_source, tmp_path)
    assert report["capture_complete"]
    assert report["revision_inventory_stable"]


def test_dataset_public_model_cache_preserves_sources(import_source):
    original = import_source.dataset
    restored = restore_model(json.loads(json.dumps(model_record(original))))
    assert restored.id == original.id
    assert [field.guid for field in restored.fields] == [field.guid for field in original.fields]
    assert [source.id for source in restored.sources] == [source.id for source in original.sources]
    assert restored.response_snapshot == original.response_snapshot


def test_binding_analysis_uses_second_dataset_not_declaration_position(import_source):
    first = replace(import_source.dataset, id="first")
    second = replace(import_source.dataset, id="second")
    guid = next(iter(second.fields)).guid
    chart = SimpleNamespace(
        dataset_ids=["first", "second"],
        data={
            "sources": {"datasetsIds": ["first", "second"], "updates": []},
            "visualization": {"measures": {"items": [{"guid": guid, "datasetId": "second"}]}},
        },
    )
    result = wizard_bindings(chart, {"first": first, "second": second})
    assert result["referenced_dataset_ids"] == ["second"]
    assert result["unused_dataset_ids"] == ["first"]
    assert not result["ambiguous"]
    del chart.data["visualization"]["measures"]["items"][0]["datasetId"]
    unresolved = wizard_bindings(chart, {"first": first, "second": second})
    assert unresolved["ambiguous"]
    assert unresolved["unresolved_dataset_ids"] == ["first", "second"]
    assert not unresolved["unused_dataset_ids"]


def test_dependency_discovery_follows_field_identity_outside_declared_list(import_source):
    first = replace(import_source.dataset, id="first")
    second = replace(import_source.dataset, id="second")
    guid = next(iter(second.fields)).guid
    chart = SimpleNamespace(
        id="chart",
        category="wizard",
        dataset_ids=["first"],
        data={
            "sources": {"datasetsIds": ["first"]},
            "visualization": {"measures": {"items": [{"guid": guid, "datasetId": "second"}]}},
        },
    )
    identifiers = set()
    chart_dependencies(chart, identifiers, {}, {"blockers": []})
    assert identifiers == {"first", "second"}
    bindings = wizard_bindings(chart, {"first": first, "second": second})
    assert bindings["declared_dataset_ids"] == ["first"]
    assert bindings["referenced_dataset_ids"] == ["second"]
    assert bindings["unused_dataset_ids"] == ["first"]


@pytest.mark.parametrize(
    "url",
    [
        "source",
        "https://datalens.ru/source-my-dashboard?tab=one",
        "https://datalens.ru/dashboards/source",
    ],
)
def test_source_identity(url):
    assert dashboard_id(url, DataLensAuthConfig()) == "source"


@pytest.mark.parametrize(
    "url",
    [
        "",
        "../source",
        "https://unrelated.example/source",
        "https://user:password@datalens.ru/source",
        "https://datalens.ru/a/b/c",
    ],
)
def test_invalid_source_identity(url):
    with pytest.raises(Exception, match=r"dashboard|Dashboard|Source"):
        dashboard_id(url, DataLensAuthConfig())


def test_inaccessible_dependency_reports_gap_and_keeps_readable_source(import_source, tmp_path):
    def denied(**kwargs):
        raise ForbiddenError(
            APIErrorContext(403, "DENIED", "read unavailable", request_id="synthetic-request")
        )

    import_source.client.get.dataset = denied
    report = run_import(import_source, tmp_path, dry_run=False)
    assert report["written"]
    assert not report["capture_complete"]
    assert any(item.get("request_id") == "synthetic-request" for item in report["capture_issues"])
    dataset = next(item for item in report["resources"].values() if item["kind"] == "dataset")
    assert dataset["capture"] == "unavailable"
    assert not dataset["editable"]
    assert not dataset["source_file"]
    assert imported_project(import_source, tmp_path).validate()["coverage"]["resource_count"] == 3


def test_drift_preserves_completed_cache_and_refreshes_on_resume(import_source, tmp_path):
    original = import_source.client.navigation.get_entries
    calls = 0

    def drift(**kwargs):
        nonlocal calls
        calls += 1
        for entry in original(**kwargs):
            if calls > 4 and entry.id == import_source.dashboard.id:
                entry.published_id = "changed"
            yield entry

    import_source.client.navigation.get_entries = drift
    with pytest.raises(Exception, match="Source inventory changed"):
        run_import(import_source, tmp_path, dry_run=False)
    assert not (tmp_path / "project").exists()
    failure = json.loads((tmp_path / "runtime/import-failure.json").read_text())
    assert failure["completed"] == 4
    assert failure["stage"] == "Verify revisions"
    import_source.client.navigation.get_entries = original
    report = run_import(import_source, tmp_path, resume=True, dry_run=False)
    assert report["written"]
    assert report["cache_hits"] >= 3


def test_returned_revision_and_id_must_match_inventory(import_source, tmp_path):
    entity = import_source.chart
    raw = copy.deepcopy(entity.raw)
    raw["revId"] = "other"
    import_source.entities[entity.id] = replace(entity, raw=raw)
    with pytest.raises(Exception, match="Published source changed"):
        run_import(import_source, tmp_path)
    import_source.entities[entity.id] = replace(entity, id="other")
    with pytest.raises(Exception, match="Returned resource identity"):
        run_import(import_source, tmp_path)


def test_source_assets_are_exact_and_contained():
    tabs = {
        "prepare": "// unicode: λ\nreturn 1;\r\n",
        "meta": "{}",
        "documentation": "# Notes\n",
        "../escape": "return 2;",
    }
    assets = source_assets(
        {"kind": "chart", "category": "editor", "metadata": {"id": "chart"}, "data": tabs}
    )
    assert sorted(assets.values()) == sorted(tabs.values())
    assert all(".." not in path and path.startswith("assets/js/source/") for path in assets)
    query = "SELECT 'λ'\r\n"
    assets = source_assets(
        {
            "kind": "dataset",
            "metadata": {"id": "data"},
            "sources": [{"id": "source", "parameters": {"subsql": query}}],
        }
    )
    assert list(assets.values()) == [query]
    cache_assets = source_assets(
        {
            "kind": "dataset",
            "metadata": {"id": "data"},
            "sources": [],
            "definition": {"cache_invalidation_source": {"mode": "sql", "sql": query}},
        }
    )
    assert list(cache_assets.values()) == [query]
    assert list(
        source_assets(
            {
                "kind": "chart",
                "category": "ql",
                "metadata": {"id": "ql"},
                "data": {"queryValue": query},
            }
        ).values()
    ) == [query]


def test_existing_destination_and_invalid_tabs_are_rejected(import_source, tmp_path):
    with pytest.raises(Exception, match="tab_ids"):
        run_import(import_source, tmp_path, tab_ids="one")
    with pytest.raises(Exception, match="absent"):
        run_import(import_source, tmp_path, tab_ids=["missing"])
    project = tmp_path / "project"
    project.mkdir()
    (project / "keep.txt").write_text("untouched")
    with pytest.raises(Exception, match="absent or empty"):
        run_import(import_source, tmp_path, dry_run=False)
    assert (project / "keep.txt").read_text() == "untouched"


def test_cross_folder_plan_and_verify_use_each_resource_location(import_source, tmp_path):
    report = run_import(import_source, tmp_path, dry_run=False)
    assert all(
        item["editable"]
        for item in report["resources"].values()
        if item["kind"] in {"dataset", "chart", "dashboard"}
    )
    project = imported_project(import_source, tmp_path)
    assert project.validate()["coverage"]["resource_count"] == 4
    plan = project.plan()
    assert not plan["conflicts"]
    verified = project.verify(full=True)
    assert not verified["mismatches"]


def test_disabled_cache_response_fields_are_semantically_equivalent(import_source, tmp_path):
    dataset = import_source.dataset
    raw = copy.deepcopy(dataset.raw)
    raw["dataset"]["cache_invalidation_source"] = {
        "mode": "off",
        "sql": None,
        "field": None,
        "filters": [],
        "cache_invalidation_error": None,
    }
    snapshot = copy.deepcopy(dataset.response_snapshot)
    snapshot["dataset"]["cache_invalidation_source"] = raw["dataset"]["cache_invalidation_source"]
    import_source.entities[dataset.id] = replace(dataset, raw=raw, response_snapshot=snapshot)
    report = run_import(import_source, tmp_path, dry_run=False)
    entry = next(item for item in report["resources"].values() if item["kind"] == "dataset")
    assert entry["editable"]
    original = json.loads((tmp_path / "project" / entry["source_file"]).read_text())
    assert (
        original["definition"]["cache_invalidation_source"]
        == raw["dataset"]["cache_invalidation_source"]
    )
    assert not imported_project(import_source, tmp_path).verify(full=True)["mismatches"]


def test_import_retains_individual_workbooks(import_source, tmp_path):
    for entity, workbook in (
        (import_source.chart, "chart-book"),
        (import_source.dataset, "data-book"),
    ):
        raw = copy.deepcopy(entity.raw)
        raw.update(workbookId=workbook, key=None)
        import_source.entities[entity.id] = replace(
            entity, location=EntryLocation.workbook(workbook), raw=raw
        )
    report = run_import(import_source, tmp_path, dry_run=False)
    by_id = {entry["id"]: entry for entry in report["resources"].values()}
    assert by_id[import_source.chart.id]["baseline_definition"]["location"] == {
        "kind": "workbook",
        "value": "chart-book",
        "reference": "id",
    }
    assert by_id[import_source.dataset.id]["baseline_definition"]["location"] == {
        "kind": "workbook",
        "value": "data-book",
        "reference": "id",
    }
    assert not imported_project(import_source, tmp_path).verify(full=True)["mismatches"]


def test_captured_dashboard_does_not_block_scoped_chart_change(import_source, tmp_path):
    dashboard = import_source.dashboard
    data = copy.deepcopy(dashboard.data)
    data["tabs"][0]["items"].append(
        {"id": "future", "type": "future_widget", "data": {"text": "retained"}}
    )
    snapshot = copy.deepcopy(dashboard.response_snapshot)
    snapshot["entry"]["data"] = data
    import_source.entities[dashboard.id] = replace(dashboard, data=data, response_snapshot=snapshot)
    report = run_import(import_source, tmp_path, dry_run=False)
    assert not report["resources"]["dashboard:main"]["editable"]
    key = next(key for key, entry in report["resources"].items() if entry["kind"] == "chart")
    assert report["resources"][key]["editable"]
    project = imported_project(import_source, tmp_path)
    plan = project.plan(resource_keys=[key])
    assert plan["write_scope"] == [key]
    assert not plan["capability_blockers"]
    assert not plan["conflicts"]
    with project.session():
        resource = load_registry().resources["dashboard:main"]
        with pytest.raises(Exception, match="Captured source write blocked"):
            guard_write(resource)


def test_scoped_pull_refuses_changed_dependency_closure_even_for_known_ids(import_source, tmp_path):
    run_import(import_source, tmp_path, dry_run=False)
    project = imported_project(import_source, tmp_path)
    data = copy.deepcopy(import_source.chart.data)
    data["sources"]["datasetsIds"].append("other")
    snapshot = copy.deepcopy(import_source.chart.response_snapshot)
    snapshot["entry"]["data"] = data
    changed = replace(import_source.chart, data=data, response_snapshot=snapshot)
    proposed = {}
    with project.session():
        registry = load_registry()
        registry.resources["dataset:other"] = SimpleNamespace(definition={"id": "other"})
        resource = next(
            item for item in registry.resources.values() if getattr(item, "kind", None) == "chart"
        )
        manifest = json.loads((tmp_path / "project/configs/import.json").read_text())
        original = copy.deepcopy(manifest)
        with pytest.raises(Exception, match="Source dependency closure changed"):
            refresh_source(resource, changed, registry, manifest, proposed)
    assert proposed == {}
    assert manifest == original


def test_manifest_prevents_reference_or_location_bypass(import_source, tmp_path):
    run_import(import_source, tmp_path, dry_run=False)
    path = tmp_path / "project/configs/DL objects/connections.json"
    value = json.loads(path.read_text())
    definition = next(iter(value.values()))
    definition["read_only"] = False
    path.write_text(json.dumps(value))
    with pytest.raises(Exception, match="read-only"):
        imported_project(import_source, tmp_path).validate()

    definition["read_only"] = True
    definition["location"]["value"] = "Moved"
    path.write_text(json.dumps(value))
    with pytest.raises(Exception, match="location changed"):
        imported_project(import_source, tmp_path).validate()


def test_corrupted_cache_is_refetched_without_losing_completed_objects(import_source, tmp_path):
    run_import(import_source, tmp_path)
    caches = list((tmp_path / "runtime/imports").glob("*/cache/*"))
    corrupt = next(
        path
        for path in caches
        if json.loads(path.read_text())["record"]["source"]["kind"] == "chart"
    )
    corrupt.write_text("{interrupted")
    before = len(import_source.reads)
    report = run_import(import_source, tmp_path, resume=True)
    assert report["capture_complete"]
    assert report["cache_hits"] >= 2
    assert any(kind == "chart" for kind, _ in import_source.reads[before:])
    assert (
        json.loads(corrupt.read_text())["record"]["source"]["metadata"]["id"]
        == import_source.chart.id
    )


def test_repeated_placements_and_hidden_tabs_are_preserved(import_source, tmp_path):
    dashboard = import_source.dashboard
    data = copy.deepcopy(dashboard.data)
    hidden = copy.deepcopy(data["tabs"][0])
    hidden.update(id="hidden", title="Hidden", hidden=True)
    data["tabs"].append(hidden)
    snapshot = copy.deepcopy(dashboard.response_snapshot)
    snapshot["entry"]["data"] = data
    import_source.entities[dashboard.id] = replace(dashboard, data=data, response_snapshot=snapshot)
    report = run_import(import_source, tmp_path, dry_run=False)
    assert report["coverage"]["tabs"] == ["one", "hidden"]
    entry = report["resources"]["dashboard:main"]
    assert json.loads((tmp_path / "project" / entry["source_file"]).read_text())["data"] == data
    assert report["coverage"]["resources"]["chart"] == 1


def test_selected_tab_capture_blocks_whole_dashboard_writer(import_source, tmp_path):
    dashboard = import_source.dashboard
    data = copy.deepcopy(dashboard.data)
    second = copy.deepcopy(data["tabs"][0])
    second.update(id="second", title="Second")
    data["tabs"].append(second)
    snapshot = copy.deepcopy(dashboard.response_snapshot)
    snapshot["entry"]["data"] = data
    import_source.entities[dashboard.id] = replace(dashboard, data=data, response_snapshot=snapshot)
    report = run_import(import_source, tmp_path, tab_ids=["one"])
    entry = report["resources"]["dashboard:main"]
    assert not entry["editable"]
    assert any(item["feature"] == "tab scope" for item in entry["restrictions"])


def test_cli_routes_preview_and_partial_capture_without_bootstrap(monkeypatch, tmp_path, capsys):
    calls = []

    def capture(source, destination, **kwargs):
        calls.append((source, destination, kwargs))
        kwargs["reporter"]("Capture: 1/2", flush=True)
        return {"capture_complete": False, "written": not kwargs["dry_run"]}

    monkeypatch.setattr(import_cli, "import_dashboard", capture)
    code = main(
        [
            "datalens",
            "import-dashboard",
            "source",
            "--destination",
            str(tmp_path),
            "--organization-id",
            "offline",
            "--json",
        ]
    )
    assert code == 2
    output = capsys.readouterr()
    assert json.loads(output.out) == {"capture_complete": False, "written": False}
    assert "Capture: 1/2" in output.err
    assert calls[0][2]["dry_run"]
