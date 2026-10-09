"""SDK-backed local validation and review regression scenarios."""

import copy
import importlib.util
import json
import sys
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from analytics_toolkit.datalens_utils import (
    BIProjectDeployment,
    DataLensProject,
    TargetLocation,
    bi_engine,
    planning,
)
from analytics_toolkit.datalens_utils.bi_dashboard import (
    add_ui,
    load_ui,
    update_endpoints,
    wiring_edges,
)
from analytics_toolkit.datalens_utils.bi_verification import dataset_issues_v2
from analytics_toolkit.datalens_utils.editing.state import fingerprint
from analytics_toolkit.datalens_utils.errors import DataLensUtilsError
from analytics_toolkit.datalens_utils.recipe import load_registry
from analytics_toolkit.datalens_utils.resources.bi_store import BIResourceStore, safe_metadata
from datalens_sdk import (
    Connection,
    Dashboard,
    DataLensClientYC,
    NoAuthProvider,
)

from tests._support.paths import REPO_ROOT
from tests.datalens_utils._support.bi import dataset, write


def test_validate_has_no_requests_or_runtime_writes(bi_project, monkeypatch):
    monkeypatch.setattr(httpx.Client, "send", Mock(side_effect=AssertionError("network")))
    bi_project.client_factory = Mock(side_effect=AssertionError("client factory"))
    assert bi_project.validate()["coverage"]["schema_version"] == 2
    assert not bi_project.paths.runtime_root.exists()


def test_plan_v2_uses_resources_baseline_and_dashboard_key(bi_project, monkeypatch):
    registry = load_registry()
    entity = dataset()
    store = BIResourceStore(registry)
    store.state["resources"]["dataset:sales"].update(
        fingerprint=fingerprint(registry.resources["dataset:sales"].files),
        metadata=safe_metadata(entity, "dataset"),
    )
    dashboard = SimpleNamespace(
        id="dashboard",
        name="Dashboard",
        key="Offline/Variants/Dashboard",
        workbook_id=None,
        saved_id="r",
        published_id="r",
        description="",
    )
    store.state["resources"]["dashboard"].update(
        fingerprint=fingerprint(registry.resources["dashboard:main"].files),
        metadata=safe_metadata(dashboard, "dashboard"),
    )
    store._save()
    write(
        bi_project.paths.runtime_root,
        "edit-state.json",
        {"resources": {"dataset:sales": {"fingerprint": "wrong"}}},
    )
    client = Mock()
    monkeypatch.setattr(planning, "datalens_client", lambda: nullcontext(client))
    monkeypatch.setattr(
        bi_engine,
        "read_entities",
        lambda *args, **kwargs: {"dataset:sales": entity, "dashboard:main": dashboard},
    )
    monkeypatch.setattr(
        "analytics_toolkit.datalens_utils.bi_preflight.operation_blockers", lambda *args: []
    )
    monkeypatch.setattr(
        "analytics_toolkit.datalens_utils.bi_verification.resource_issues", lambda *args: []
    )
    before = store.path.read_bytes()
    result = planning.plan_v2(registry, resource_keys=["dataset:sales"])
    statuses = {value["resource"]: value["action"] for value in result["actions"]}
    assert statuses["dataset:sales"] == statuses["dashboard:main"] == "unchanged"
    assert store.path.read_bytes() == before
    altered = copy.deepcopy(registry.resources["dataset:sales"].files)
    altered["assets/sql.sql"] = "select 2"
    old = store.state["resources"]["dataset:sales"]
    assert (
        planning.classify(fingerprint(altered), old, safe_metadata(entity, "dataset")) == "update"
    )
    entity.published_id = entity.saved_id = "remote"
    assert planning.classify(old["fingerprint"], old, safe_metadata(entity, "dataset")) == "pull"
    assert (
        planning.classify(fingerprint(altered), old, safe_metadata(entity, "dataset")) == "conflict"
    )


def test_affected_dashboard_reads_unrelated_chart_and_dataset(bi_project):
    root = bi_project.paths.project_root
    charts = json.loads((root / "configs/DL objects/charts/wizard/line/trend.json").read_text())
    charts.update(key="other", id="other", name="Other", title="Other")
    write(root, "configs/DL objects/charts/wizard/line/other.json", charts)
    registry = load_registry()
    reads, writes, affected = registry.read_closure({"chart:trend"})
    assert "chart:other" in reads
    assert "dataset:sales" in reads
    assert writes == {"chart:trend", "dashboard:main"}
    assert affected == ["dashboard:main"]


def test_unapplied_formula_verification_reports_mismatch(bi_project):
    registry = load_registry()
    definition = copy.deepcopy(registry.resources["dataset:sales"].definition)
    definition["calculations"]["Amount"]["formula"] = "SUM(2)"
    with DataLensClientYC(auth=NoAuthProvider()) as client:
        issues = dataset_issues_v2(
            client,
            dataset(),
            definition,
            {},
            {"ch": Connection(id="ch", name="CH", type="clickhouse")},
        )
    assert "Amount formula" in issues


def test_wizard_parameter_override_explicitly_rejected(bi_project):
    root = bi_project.paths.project_root
    path = "configs/DL objects/charts/wizard/line/trend.json"
    value = json.loads((root / path).read_text())
    value["params"] = {"period_type": ["month"]}
    write(root, path, value)
    with pytest.raises(DataLensUtilsError, match=r"wizard\.parameters\.update"):
        bi_project.validate()
    assert not bi_project.paths.runtime_root.exists()


def test_member_wiring_keeps_variant_identity(bi_project):
    root = bi_project.paths.project_root
    write(
        root,
        "configs/UI/layout.json",
        {"main": {"variants": [0, 0, 12, 6], "region_control": [0, 6, 12, 2]}},
    )
    write(
        root,
        "configs/UI/chart_groups.json",
        {
            "variants": {
                "tab": "main",
                "charts": [
                    {"key": "a", "chart": "trend", "params": {"period_type": ["month"]}},
                    {"key": "b", "chart": "trend", "params": {"period_type": ["year"]}},
                ],
            }
        },
    )
    write(
        root,
        "configs/UI/selectors/region.json",
        {
            "key": "region",
            "tab": "main",
            "title": "Region",
            "source": {"kind": "dataset", "dataset": "sales", "field": "category"},
            "control": {"element": "select"},
        },
    )
    write(root, "configs/UI/links/connections.json", {"region": ["variants/a"]})
    definitions = {
        key.split(":", 1)[1]: resource.definition
        for key, resource in load_registry().resources.items()
        if resource.kind == "chart"
    }
    ui = load_ui(definitions)
    update = Dashboard(
        id="dashboard", data={"tabs": [{"id": "main", "title": "Main", "items": [], "layout": []}]}
    ).update
    add_ui(update, ui, {"trend": SimpleNamespace(id="trend")}, {"sales": dataset()}, definitions)
    endpoints = update_endpoints(ui, update)
    edges = wiring_edges(ui, "main", definitions, endpoints)
    assert (endpoints["variants/b"][0], "region") in edges
    assert (endpoints["variants/a"][0], "region") not in edges
    for receiver, sender in edges:
        update.add_connection(from_item=receiver, to_item=sender, tab="main")
    assert update.to_spec().ops


def test_version2_scaffold_and_advanced_example_are_local(tmp_path, monkeypatch):
    script = REPO_ROOT / ".agents/skills/datalens-dashboard/scripts/scaffold.py"
    spec = importlib.util.spec_from_file_location("bi_scaffold", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    root = tmp_path / "scaffold"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "scaffold",
            str(root),
            "--table",
            "example.sales",
            "--schema-version",
            "2",
            "--installation",
            "enterprise",
            "--base-url",
            "https://bi.example.test",
            "--workbook-id",
            "workbook",
            "--connector",
            "postgres",
            "--connection-name",
            "Offline Postgres",
            "--connection-id",
            "offline-postgres",
            "--source-factory",
            "pg_table",
            "--source-parameters",
            '{"db_name":"example","table_name":"sales"}',
        ],
    )
    module.main()
    spec = importlib.util.spec_from_file_location("bi_launcher", root / "dashboard.py")
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    project = launcher.make_project(client_factory=Mock(side_effect=AssertionError("network")))
    assert project.validate()["coverage"]["schema_version"] == 2
    assert project.deployment.target.value == "workbook"
    assert not (root / ".venv").exists()
    assert not project.paths.runtime_root.exists()
    project = DataLensProject(
        REPO_ROOT / "examples/datalens_bi",
        tmp_path / "example-runtime",
        BIProjectDeployment(
            "BI example", TargetLocation.path("Offline/BI"), organization_id="offline"
        ),
    )
    assert project.validate()["coverage"]["schema_version"] == 2
    assert not project.paths.runtime_root.exists()
