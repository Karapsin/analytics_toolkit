"""Published verification, configured IDs, and export completeness."""

import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from analytics_toolkit.datalens_utils.errors import DataLensUtilsError
from analytics_toolkit.datalens_utils.validation import checks


def test_manual_bindings_require_all_ql_receivers():
    contents = {
        "ql": {
            "selectors": {
                "a": {
                    "key": "a",
                    "source": {"kind": "manual", "param_name": "region"},
                    "recipients": ["trend"],
                },
                "b": {"key": "b", "source": {"kind": "dataset"}, "recipients": []},
            },
            "selector_groups": {
                "g": {
                    "definitions": {
                        "c": {
                            "key": "c",
                            "source": {"kind": "manual", "param_name": "missing"},
                            "recipients": ["trend"],
                        },
                        "d": {
                            "key": "d",
                            "source": {"kind": "manual", "param_name": "region"},
                            "recipients": [],
                        },
                    }
                }
            },
        }
    }
    assert checks._declared_manual_bindings(
        contents, {"trend": {"family": "ql", "params": [{"name": "region"}]}}
    ) == {"a"}


@pytest.mark.parametrize("problem", ["dataset", "chart", "dashboard", "refs", "valid"])
def test_published_verification_rejects_mismatches_before_export(session_state, problem):
    client = Mock()
    dataset = SimpleNamespace(id="dataset", name="Sales")
    chart = SimpleNamespace(id="chart", name="Trend")
    dashboard = SimpleNamespace(id="dashboard")
    client.get.dataset.return_value = dataset
    client.get.dashboard.return_value = dashboard
    resources = Mock()
    context = SimpleNamespace(client=client, resources=resources)
    issue = SimpleNamespace(kind="bad", item_id="other")
    with patch.object(
        checks, "dataset_issues", return_value=["bad"] if problem == "dataset" else []
    ), patch.object(checks, "chart_getter", return_value=Mock(return_value=chart)), patch.object(
        checks, "check_chart", return_value=["bad"] if problem == "chart" else []
    ), patch.object(
        checks, "dashboard_issues", return_value=["bad"] if problem == "dashboard" else []
    ), patch.object(
        checks.recipes, "validate_dashboard_refs", return_value=[issue] if problem == "refs" else []
    ), patch.object(checks, "export_bundle", return_value="offline-export") as export:
        kwargs = {
            "context": context,
            "dashboard": dashboard,
            "datasets": {"sales": dataset},
            "charts": {"trend": chart},
            "dataset_definitions": {"sales": {"name": "Sales"}},
            "chart_definitions": {"trend": {"name": "Trend"}},
            "tab_definitions": {},
            "contents": {},
            "description": "",
            "hide_tabs": False,
        }
        if problem == "valid":
            assert checks.verify_and_export(**kwargs) is dashboard
            assert context.verified_datasets == {"sales": dataset}
            resources.complete_target.assert_called_once()
        else:
            with pytest.raises(DataLensUtilsError, match="Published"):
                checks.verify_and_export(**kwargs)
            export.assert_not_called()


def test_configured_ids_roundtrip_and_unchanged_second_write(session_state):
    root = session_state.paths.project_root / "configs/DL objects"
    datasets = json.loads((root / "datasets.json").read_text())
    charts = {}
    for area in ("charts", "selectors"):
        for path in (root / area).rglob("*.json"):
            definition = json.loads(path.read_text())
            charts[definition.get("key", path.stem)] = SimpleNamespace(id="id-" + path.stem)
    datasets = {key: SimpleNamespace(id="id-" + key) for key in datasets}
    dashboard = SimpleNamespace(id="dashboard")
    checks.record_configured_ids(dashboard, datasets=datasets, charts=charts)
    assert json.loads((root / "dashboard.json").read_text())["id"] == "dashboard"
    with patch("pathlib.Path.replace") as replace:
        checks.record_configured_ids(dashboard, datasets=datasets, charts=charts)
    replace.assert_not_called()


def test_export_missing_artifact_and_stale_manifest(session_state):
    value = SimpleNamespace(id="dashboard", rev_id="revision", key="Offline/Dashboard")
    value.to_file = lambda path: path
    with pytest.raises(DataLensUtilsError, match="expected artifact"):
        checks.export_bundle(value, datasets={}, charts={})

    def export(path):
        (path / "dashboard.json").write_text("{}", encoding="utf-8")
        return path

    value.to_file = export
    destination = checks.export_bundle(value, datasets={}, charts={})
    (destination / "manifest.json").write_text('{"resources": {}, "files": []}', encoding="utf-8")
    assert checks.export_bundle(value, datasets={}, charts={}) == destination
    (destination / "dashboard/dashboard.json").unlink()
    assert checks.export_bundle(value, datasets={}, charts={}) == destination
