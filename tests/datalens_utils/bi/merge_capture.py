"""Existing recipes use the same revision-aware preserved capture as new imports."""

import copy
import json
from contextlib import nullcontext
from dataclasses import replace

import pytest
from analytics_toolkit.datalens_utils.bi_import import discover_dependencies
from analytics_toolkit.datalens_utils.errors import DataLensUtilsError
from analytics_toolkit.datalens_utils.import_capture import Capture
from analytics_toolkit.datalens_utils.import_source import digest, source_document
from analytics_toolkit.datalens_utils.local_transactions import snapshot
from datalens_sdk import APIErrorContext, ForbiddenError


def configure(project, source):
    project.client_factory = lambda _: nullcontext(source.client)
    return source.dashboard.id


def test_merge_preserves_snapshot_and_resumes_without_duplicate_getters(bi_project, import_source):
    identifier = configure(bi_project, import_source)
    connection = next(
        value for value in import_source.entities.values() if type(value).__name__ == "Connection"
    )
    import_source.entities[connection.id] = replace(connection, raw={"saved_id": "connection-r"})
    root = bi_project.paths.project_root
    before = snapshot(root)
    preview = bi_project.import_dashboard(dashboard_id=identifier)
    assert not preview["blockers"], preview["blockers"]
    assert preview["capture_complete"]
    assert preview["revision_inventory_stable"]
    assert snapshot(root) == before
    reads = list(import_source.reads)
    merged = bi_project.import_dashboard(dashboard_id=identifier, dry_run=False)
    assert merged["written"], merged["blockers"]
    assert merged["cache_hits"] == 4
    assert import_source.reads == reads
    manifest = json.loads((root / merged["capture_manifest"]).read_text())
    assert manifest["purpose"] == "import-provenance"
    assert len(manifest["resources"]) == 4
    for entry in manifest["resources"].values():
        captured = json.loads((root / entry["source_file"]).read_text())
        expected = source_document(import_source.entities[entry["id"]], entry["kind"])
        assert captured == json.loads(json.dumps(expected))
        assert digest(captured) == entry["source_hash"]
    assert not (root / "configs/import.json").exists()
    assert json.loads((root / "configs/DL objects/dashboard.json").read_text())["id"] == "dashboard"


def test_merge_reads_published_despite_newer_saved_draft(bi_project, import_source):
    identifier = configure(bi_project, import_source)
    import_source.entities[identifier] = replace(import_source.dashboard, saved_id="new-draft")
    report = bi_project.import_dashboard(dashboard_id=identifier)
    assert report["capture_complete"]
    assert all(
        options.get("branch") == "published"
        for kind, options in import_source.reads
        if kind != "connection"
    )


def test_legacy_discovery_helper_remains_compatible(bi_project, import_source):
    report = {"blockers": []}
    result = discover_dependencies(import_source.client, import_source.dashboard, {"one"}, report)
    assert list(result.charts) == [import_source.chart.id]
    assert list(result.datasets) == [import_source.dataset.id]
    assert list(result.connections) == [import_source.dataset.sources[0].connection_id]
    assert not report["blockers"]


def test_capture_reuses_already_read_entity_within_one_snapshot(bi_project, import_source):
    capture = Capture(import_source.client)
    capture.discover(import_source.dashboard.id)
    reads = list(import_source.reads)
    assert capture.read(import_source.chart.id) is import_source.chart
    assert import_source.reads == reads


@pytest.mark.parametrize("phase", ["returned-revision", "ending-inventory"])
def test_merge_drift_keeps_destination_and_completed_capture(bi_project, import_source, phase):
    identifier = configure(bi_project, import_source)
    before = snapshot(bi_project.paths.project_root)
    if phase == "returned-revision":
        import_source.entities[identifier] = replace(import_source.dashboard, rev_id="changed")
    else:
        entries = import_source.client.navigation.get_entries
        calls = []

        def drift(**kwargs):
            calls.append(kwargs)
            values = list(entries(**kwargs))
            if len(calls) == 5:
                values[0].published_id = "changed"
            return values

        import_source.client.navigation.get_entries = drift
    with pytest.raises(DataLensUtilsError, match="changed"):
        bi_project.import_dashboard(dashboard_id=identifier, dry_run=False)
    assert snapshot(bi_project.paths.project_root) == before
    failure = json.loads((bi_project.paths.runtime_root / "import-failure.json").read_text())
    assert failure["stage"] in {"Capture", "Verify revisions"}
    if phase == "ending-inventory":
        assert failure["completed"] == 4
        assert list(bi_project.paths.runtime_root.glob("imports/*/cache/*"))


def test_merge_inventory_gap_reports_incomplete_source_and_preserves_files(
    bi_project, import_source
):
    identifier = configure(bi_project, import_source)
    entries = import_source.client.navigation.get_entries
    import_source.client.navigation.get_entries = lambda **kwargs: [
        entry for entry in entries(**kwargs) if entry.id != import_source.chart.id
    ]
    before = snapshot(bi_project.paths.project_root)
    report = bi_project.import_dashboard(dashboard_id=identifier, dry_run=False)
    assert not report["written"]
    assert not report["capture_complete"]
    assert not report["revision_inventory_stable"]
    assert report["capture_issues"]
    assert snapshot(bi_project.paths.project_root) == before
    assert report["proposed_files"][report["capture_manifest"]]["resources"]


def test_merge_unsupported_readable_source_is_retained_in_preview(bi_project, import_source):
    identifier = configure(bi_project, import_source)
    chart = import_source.chart
    data = copy.deepcopy(chart.data)
    data["visualization"]["type"] = "unknown-writer"
    response = copy.deepcopy(chart.response_snapshot)
    response["entry"]["data"] = data
    import_source.entities[chart.id] = replace(chart, data=data, response_snapshot=response)
    report = bi_project.import_dashboard(dashboard_id=identifier, dry_run=False)
    assert not report["written"]
    assert report["capture_complete"]
    manifest = report["proposed_files"][report["capture_manifest"]]
    source = next(entry for entry in manifest["resources"].values() if entry["id"] == chart.id)
    assert report["proposed_files"][source["source_file"]]["data"] == data
    assert report["blockers"]


def test_merge_unavailable_dashboard_failure_has_request_identity(bi_project, import_source):
    identifier = configure(bi_project, import_source)

    def denied(**kwargs):
        raise ForbiddenError(APIErrorContext(403, "DENIED", "no source", request_id="read-request"))

    import_source.client.get.dashboard = denied
    with pytest.raises(DataLensUtilsError, match="inaccessible"):
        bi_project.import_dashboard(dashboard_id=identifier)
    failure = json.loads((bi_project.paths.runtime_root / "import-failure.json").read_text())
    assert failure["capture_issues"][0]["request_id"] == "read-request"


def test_merge_refuses_destination_edit_during_capture(bi_project, import_source):
    identifier = configure(bi_project, import_source)
    entries = import_source.client.navigation.get_entries
    target = bi_project.paths.project_root / "local-note.md"

    def edit(**kwargs):
        target.write_text("edited during capture")
        return entries(**kwargs)

    import_source.client.navigation.get_entries = edit
    before_tabs = (bi_project.paths.project_root / "configs/UI/tabs.json").read_bytes()
    with pytest.raises(DataLensUtilsError, match="destination changed"):
        bi_project.import_dashboard(dashboard_id=identifier, dry_run=False)
    assert target.read_text() == "edited during capture"
    assert (bi_project.paths.project_root / "configs/UI/tabs.json").read_bytes() == before_tabs


def test_merge_unavailable_chart_retains_other_source_and_reports_gap(bi_project, import_source):
    identifier = configure(bi_project, import_source)

    def denied(**kwargs):
        raise ForbiddenError(APIErrorContext(403, "DENIED", "no chart", request_id="chart-request"))

    import_source.client.get.chart = denied
    report = bi_project.import_dashboard(dashboard_id=identifier, dry_run=False)
    assert not report["written"]
    assert not report["capture_complete"]
    manifest = report["proposed_files"][report["capture_manifest"]]
    unavailable = next(
        entry for entry in manifest["resources"].values() if entry["id"] == import_source.chart.id
    )
    assert unavailable["capture"] == "unavailable"
    assert unavailable["source_file"] is None
    assert any(
        entry["kind"] == "dashboard" and entry["capture"] == "complete"
        for entry in manifest["resources"].values()
    )
