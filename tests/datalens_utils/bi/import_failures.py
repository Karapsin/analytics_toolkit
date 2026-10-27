"""Import consistency failures preserve evidence and never publish partial files."""

import json
from dataclasses import replace

import pytest
from analytics_toolkit.cli import main
from analytics_toolkit.datalens_utils import DataLensAuthConfig, import_cli, import_dashboard
from analytics_toolkit.datalens_utils.errors import DataLensConfigurationError, DataLensUtilsError
from analytics_toolkit.datalens_utils.import_api import dashboard_id
from analytics_toolkit.datalens_utils.import_source import digest, restore_model
from datalens_sdk import APIErrorContext, ForbiddenError

from tests.datalens_utils._support.captured_import import imported_project, run_import


def denied():
    return ForbiddenError(APIErrorContext(403, "DENIED", "Unavailable", request_id="request-403"))


def test_enterprise_source_uses_configured_host():
    auth = DataLensAuthConfig(installation="enterprise", base_url="https://bi.example")
    assert dashboard_id("https://bi.example/dashboard/retained-name", auth) == "retained"
    with pytest.raises(DataLensConfigurationError, match="configured installation"):
        dashboard_id("https://datalens.ru/retained", auth)


def test_invalid_auth_is_rejected_before_local_or_remote_work(tmp_path):
    with pytest.raises(DataLensConfigurationError, match="auth must"):
        import_dashboard("source", tmp_path / "project", auth={})
    assert not (tmp_path / "project").exists()


def test_missing_source_location_keeps_capture_but_refuses_project(import_source, tmp_path):
    dashboard = import_source.dashboard
    import_source.entities[dashboard.id] = replace(dashboard, location=None, raw={})
    with pytest.raises(DataLensConfigurationError, match="Source dashboard location"):
        run_import(import_source, tmp_path, dry_run=False)
    failure = json.loads((tmp_path / "runtime/import-failure.json").read_text())
    assert failure["completed"] == 4
    assert failure["resource"] == "dashboard:" + dashboard.id
    assert list((tmp_path / "runtime/imports").glob("*/cache/*"))
    assert not (tmp_path / "project").exists()


@pytest.mark.parametrize("failed_inventory", [1, 5])
def test_inventory_permission_gap_is_distinct_from_source_capture(
    import_source, tmp_path, failed_inventory
):
    original = import_source.client.navigation.get_entries
    calls = 0

    def inventory(**kwargs):
        nonlocal calls
        calls += 1
        if calls == failed_inventory:
            raise denied()
        return original(**kwargs)

    import_source.client.navigation.get_entries = inventory
    report = run_import(import_source, tmp_path, dry_run=False)
    assert report["written"]
    assert not report["capture_complete"]
    assert not report["revision_inventory_stable"]
    assert report["coverage"]["captured"] == 4
    assert any(issue.get("request_id") == "request-403" for issue in report["capture_issues"])


def test_missing_published_revision_is_explicitly_unverified(import_source, tmp_path):
    original = import_source.client.navigation.get_entries

    def inventory(**kwargs):
        for entry in original(**kwargs):
            if entry.id == import_source.chart.id:
                entry.published_id = None
            yield entry

    import_source.client.navigation.get_entries = inventory
    report = run_import(import_source, tmp_path)
    assert not report["capture_complete"]
    assert any(issue["feature"] == "revision" for issue in report["capture_issues"])
    assert not (tmp_path / "project").exists()


@pytest.mark.parametrize("kind", ["dataset", "chart"])
def test_incomplete_sdk_source_is_reported_without_discarding_other_resources(
    import_source, tmp_path, kind
):
    entity = getattr(import_source, kind)
    import_source.entities[entity.id] = replace(entity, response_snapshot={})
    report = run_import(import_source, tmp_path, dry_run=False)
    resource = next(item for item in report["resources"].values() if item["id"] == entity.id)
    assert not report["capture_complete"]
    assert resource["capture"] == "unavailable"
    assert not resource["editable"]
    assert not resource["source_file"]
    assert report["coverage"]["captured"] == 3
    assert imported_project(import_source, tmp_path).validate()["coverage"]["resource_count"] == 4


def test_inaccessible_root_keeps_failure_evidence(import_source, tmp_path):
    def unavailable(**kwargs):
        raise denied()

    import_source.client.get.dashboard = unavailable
    with pytest.raises(DataLensUtilsError, match="Source dashboard is inaccessible"):
        run_import(import_source, tmp_path, dry_run=False)
    failure = json.loads((tmp_path / "runtime/import-failure.json").read_text())
    assert failure["completed"] == 0
    assert failure["resource"] == "dashboard:" + import_source.dashboard.id
    assert failure["capture_issues"][0]["request_id"] == "request-403"
    assert not (tmp_path / "project").exists()


@pytest.mark.parametrize("corruption", ["identity", "model-id", "model-type"])
def test_tampered_cache_is_refetched_by_revision(import_source, tmp_path, corruption):
    run_import(import_source, tmp_path)
    caches = list((tmp_path / "runtime/imports").glob("*/cache/*"))
    path = next(
        p for p in caches if json.loads(p.read_text())["identity"]["id"] == import_source.chart.id
    )
    payload = json.loads(path.read_text())
    if corruption == "identity":
        payload["identity"]["name"] = "not the current source"
    elif corruption == "model-id":
        payload["record"]["model"]["values"]["id"] = "wrong-resource"
    else:
        payload["record"]["model"]["type"] = "UnknownSDKModel"
    payload["digest"] = digest(payload["record"])
    path.write_text(json.dumps(payload))
    import_source.reads.clear()
    report = run_import(import_source, tmp_path, resume=True)
    assert report["capture_complete"]
    assert report["cache_hits"] == 2
    # The fixture's unversioned connection is always re-read; cached datasets and
    # dashboard stay reusable while the modified chart is refreshed.
    assert [args["by_id"] for _, args in import_source.reads] == [
        import_source.chart.id,
        import_source.dataset.sources[0].connection_id,
    ]


def test_unknown_cached_model_is_rejected():
    with pytest.raises(DataLensConfigurationError, match="Unknown cached public read model"):
        restore_model({"type": "Client", "values": {}})


@pytest.mark.parametrize("mutation", ["version", "identity", "source_ref", "asset"])
def test_manifest_tampering_is_rejected_offline(import_source, tmp_path, mutation):
    run_import(import_source, tmp_path, dry_run=False)
    root = tmp_path / "project"
    manifest_path = root / "configs/import.json"
    manifest = json.loads(manifest_path.read_text())
    key, entry = next(
        (key, entry) for key, entry in manifest["resources"].items() if entry["kind"] == "dataset"
    )
    if mutation == "version":
        manifest["version"] = 999
    elif mutation == "asset":
        valid = "assets/sql/source/retained.sql"
        (root / valid).parent.mkdir(parents=True, exist_ok=True)
        (root / valid).write_text("original")
        entry["asset_hashes"][valid] = digest("original")
        path = "assets/sql/source/tampered.sql"
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text("changed")
        entry["asset_hashes"][path] = digest("original")
    else:
        objects = root / "configs/DL objects/datasets.json"
        definitions = json.loads(objects.read_text())
        definitions[key.split(":", 1)[1]]["id" if mutation == "identity" else "source_ref"] = (
            "missing"
        )
        objects.write_text(json.dumps(definitions))
        if mutation == "source_ref":
            del manifest["resources"][key]
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(DataLensConfigurationError, match=r"manifest|identity|integrity"):
        imported_project(import_source, tmp_path).validate()


def test_empty_existing_destination_is_published_atomically(import_source, tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    report = run_import(import_source, tmp_path, dry_run=False)
    assert report["written"]
    assert (root / "dashboard.py").is_file()
    assert not list(tmp_path.glob(".datalens-import-*"))


def test_cli_returns_typed_sdk_failure_with_request_id(monkeypatch, tmp_path, capsys):
    def fail(*args, **kwargs):
        raise denied()

    monkeypatch.setattr(import_cli, "import_dashboard", fail)
    assert (
        main(["datalens", "import-dashboard", "source", "--destination", str(tmp_path), "--json"])
        == 1
    )
    report = json.loads(capsys.readouterr().out)
    assert not report["written"]
    assert report["error"]["type"] == "ForbiddenError"
    assert report["error"]["request_id"] == "request-403"


def test_html_dependency_is_reported_without_guessing_its_authored_source(import_source, tmp_path):
    original = import_source.client.navigation.get_entries

    def inventory(**kwargs):
        for entry in original(**kwargs):
            if entry.id == import_source.chart.id:
                entry.scope = "artifact"
            yield entry

    import_source.client.navigation.get_entries = inventory
    report = run_import(import_source, tmp_path, dry_run=False)
    assert not report["capture_complete"]
    html = next(item for item in report["resources"].values() if item["kind"] == "html_page")
    assert html["capture"] == "unavailable"
    assert html["source_file"] is None
    assert any("HTML authored-source" in issue["reason"] for issue in report["capture_issues"])
    assert not any(args["by_id"] == import_source.chart.id for _, args in import_source.reads)
