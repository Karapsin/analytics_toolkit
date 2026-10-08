"""Incremental routing uses one inventory and preserves scope and verification."""

import json
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from analytics_toolkit.datalens_utils.editing import commands
from analytics_toolkit.datalens_utils.errors import DataLensUtilsError

from tests.datalens_utils._support.sdk import configured_datasets


@pytest.mark.parametrize(
    ("chart", "dataset", "ui", "expected"),
    [
        (["trend"], None, False, {"chart:trend"}),
        (None, ["sales"], False, {"dataset:sales"}),
        (None, None, True, {"dashboard"}),
        (None, None, False, {"chart:trend", "dataset:sales", "dashboard"}),
    ],
)
def test_selection_matches_public_scope(chart, dataset, ui, expected):
    args = SimpleNamespace(chart=chart, dataset=dataset, ui=ui)
    assert commands.selection(args, {key: {} for key in expected}) == expected
    args.chart = ["unknown"]
    with pytest.raises(DataLensUtilsError, match="Unknown resource"):
        commands.selection(args, {"dashboard": {}})


@pytest.mark.parametrize(
    "scenario",
    ["status", "verify", "pull", "pull noop", "pull dataset", "apply", "baseline", "missing"],
)
def test_incremental_command_routing_uses_one_revision_inventory(session_state, scenario):  # noqa: C901 - Command scenario dispatch.
    units = commands.configuration()
    for key, unit in units.items():
        unit["id"] = key
    entries = {
        key: SimpleNamespace(
            id=key, key="Offline/" + key, saved_id="rev", published_id="rev", is_locked=False
        )
        for key in units
    }
    changes = dict.fromkeys(units, "unchanged")
    command = scenario.split()[0]
    if scenario in {"status", "pull", "apply", "baseline"}:
        changes["chart:wizard_line"] = "untracked" if scenario == "baseline" else "local"
    if scenario == "pull dataset":
        changes["dataset:retail"] = "remote"
    if scenario == "missing":
        entries["dashboard"] = None
        command = "verify"
    if scenario == "baseline":
        command = "apply"
    args = SimpleNamespace(command=command, chart=None, dataset=None, ui=False, branch="published")
    cache = Mock()
    cache.changes.side_effect = [changes, dict.fromkeys(units, "unchanged")]
    client = Mock()
    with patch.object(commands, "configuration", return_value=units), patch.object(
        commands, "EditState", return_value=cache
    ), patch.object(commands, "datalens_client", return_value=nullcontext(client)), patch.object(
        commands, "inventory", return_value=entries
    ) as inventory, patch.object(commands, "context_for", return_value=Mock()), patch.object(
        commands, "full_verify"
    ) as verify, patch.object(commands, "import_changes") as pull, patch.object(
        commands, "apply_changes"
    ) as apply:
        if scenario == "missing":
            with pytest.raises(DataLensUtilsError, match="missing"):
                commands.run(args, session_state.deployment.as_dict())
        else:
            report = commands.run(args, session_state.deployment.as_dict())
            expected = dict.fromkeys(units, "unchanged") if scenario == "baseline" else changes
            assert report["resource_statuses"] == expected
    inventory.assert_called_once()
    if scenario in {"verify", "baseline"}:
        verify.assert_called_once()
    if scenario in {"pull", "pull dataset"}:
        pull.assert_called_once()
        if scenario == "pull dataset":
            assert "dashboard" in pull.call_args.args[2]
    if scenario == "apply":
        apply.assert_called_once()
    if scenario == "pull noop":
        assert "No remote changes to pull." in session_state.messages


def test_fetch_and_incremental_context_resolve_only_existing_folders(session_state):
    client = Mock()
    folders = {
        "Offline/Dashboards": Mock(key="Offline/Dashboards/"),
        "Offline/Dashboards/charts": Mock(key="Offline/Dashboards/charts/"),
        "Offline/Dashboards/datasets": Mock(key="Offline/Dashboards/datasets/"),
    }
    for folder in folders.values():
        folder.list_entries.return_value = []
    client.get.folder.side_effect = lambda **options: folders[options["by_path"]]
    connection = SimpleNamespace(name="CH", type="clickhouse")
    client.get.connection.return_value = connection
    settings = {"folders": {"charts": "charts", "datasets": "datasets"}}
    context = commands.context_for(
        client,
        session_state.deployment.as_dict(),
        {"missing": None},
        {"sales": {"table": "example.sales"}},
        settings,
    )
    assert context.folder is folders["Offline/Dashboards"]
    assert context.source_tables == {"sales": "example.sales"}
    connection.name = "Other"
    with pytest.raises(DataLensUtilsError, match="identity"):
        commands.context_for(
            client,
            session_state.deployment.as_dict(),
            {},
            {"sales": {"table": "example.sales"}},
            settings,
        )
    unit = {"id": "stable"}
    assert commands.fetch(client, "dashboard", unit, {}) is client.get.dashboard.return_value
    assert commands.fetch(client, "dataset:sales", unit, {}) is client.get.dataset.return_value
    with patch.object(commands, "chart_getter", return_value=client.get.chart):
        assert (
            commands.fetch(client, "chart:trend", unit, {"trend": {}})
            is client.get.chart.return_value
        )


def test_query_acceptance_deduplicates_equal_dataset_projection(session_state):
    definitions = {"sales": {"name": "Sales", "fields": {"region": {"cast": "string"}}}}
    dataset = configured_datasets(definitions)["sales"]
    proxy = Mock()
    proxy.id = dataset.id
    proxy.fields = dataset.fields
    proxy.find_field.side_effect = dataset.find_field
    charts = {
        "a": {
            "family": "wizard",
            "dataset": "sales",
            "fields": {"x": ["region", "local"]},
            "filters": [{"field": "region", "operation": "IN", "values": ["North"]}],
        },
        "b": {
            "family": "wizard",
            "dataset": "sales",
            "fields": {"x": ["region", "local"]},
            "filters": [{"field": "region", "operation": "IN", "values": ["North"]}],
        },
        "c": {"family": "ql"},
        "empty": {"family": "wizard", "dataset": "sales", "fields": {}},
    }
    commands.check_queries({"sales": proxy}, {}, charts, charts)
    proxy.get_dataset_data.assert_called_once()
    assert any("cannot execute" in value for value in session_state.messages)
    assert any("Dataset query checks: 1" in value for value in session_state.messages)


def test_full_verification_and_baseline_remember_each_entity(session_state):
    context = SimpleNamespace(client=Mock())
    cache = Mock()
    units = {"dashboard": {}, "dataset:sales": {}, "chart:trend": {}}
    values = {key: SimpleNamespace(id=key) for key in units}
    with patch.object(
        commands, "fetch", side_effect=lambda client, key, *args, **kwargs: values[key]
    ), patch.object(commands, "verify_and_export", return_value=values["dashboard"]) as verify:
        assert (
            commands.full_verify(context, units, {"sales": {}}, {"trend": {}}, {}, cache)
            is values["dashboard"]
        )
    assert cache.remember.call_count == 3
    assert verify.call_args.kwargs["refetch"] is False
    with patch.object(commands, "EditState", return_value=cache), patch.object(
        commands, "configuration", return_value=units
    ):
        commands.remember_full(
            session_state.deployment.as_dict(),
            values["dashboard"],
            {"sales": values["dataset:sales"]},
            {"trend": values["chart:trend"]},
        )
    assert cache.remember.call_count == 6


@pytest.mark.parametrize("problem", ["chart", "dataset", "dashboard", "geometry"])
def test_import_validation_never_replaces_original_files(session_state, problem):
    charts = commands.read_chart_definitions()
    context = SimpleNamespace(entries={"chart:" + key: SimpleNamespace(id=key) for key in charts})
    key = (
        "chart:wizard_line"
        if problem == "chart"
        else "dataset:retail"
        if problem == "dataset"
        else "dashboard"
    )
    issues = (
        [SimpleNamespace(kind="overlap", message="Imported overlap")]
        if problem == "geometry"
        else ["unsupported"]
    )
    path = session_state.paths.project_root / "configs/UI/texts.json"
    original = path.read_text()
    files = {"configs/UI/texts.json": json.loads(original)}
    with patch.object(commands, "check_chart", return_value=issues), patch.object(
        commands, "dataset_issues", return_value=issues
    ), patch.object(commands, "dashboard_issues", return_value=issues):
        if problem == "geometry":
            commands.validate_import(files, context, {}, {key: SimpleNamespace()})
        else:
            with pytest.raises(DataLensUtilsError, match="No files changed"):
                commands.validate_import(files, context, {}, {key: SimpleNamespace()})
    assert path.read_text() == original
