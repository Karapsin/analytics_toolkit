"""Capabilities and connection identity are verified before folder writes."""

from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from analytics_toolkit.datalens_utils.errors import DataLensUtilsError
from analytics_toolkit.datalens_utils.resources import context


def arguments():
    return {
        "dashboard_path": "Offline/Dashboard",
        "source_tables": {"sales": "example.sales"},
        "resource_folders": {"charts": "charts", "datasets": "datasets"},
        "connection_name": "CH",
        "connection_id": "conn",
        "chart_definitions": {
            "trend": {"family": "wizard", "type": "line", "name": "Trend", "id": "trend"}
        },
        "dataset_definitions": {"sales": {"name": "Sales", "id": "sales"}},
        "dashboard_id": "dashboard",
    }


def test_context_seeds_ids_and_supplies_explicit_sources(session_state):
    client = Mock()
    client.capabilities = {
        "dataset_sources": ["CH_TABLE"],
        "chart_factories": {"wizard": ["line"], "ql": [], "editor": []},
    }
    client.get.connection.return_value = SimpleNamespace(
        id="conn", name="CH", type="clickhouse", raw={"raw_sql_level": "dashsql"}
    )
    folder = Mock(key="Offline/")
    folder.list_entries.return_value = []
    with patch.object(context, "datalens_client", return_value=nullcontext(client)), patch.object(
        context, "ensure_target_folder", return_value=folder
    ), patch.object(
        context,
        "ensure_resource_folders",
        return_value=dict.fromkeys(("dash", "widget", "dataset"), folder),
    ), context.dashboard_context(**arguments()) as actual:
        assert actual.source_tables == {"sales": "example.sales"}
        assert actual.resources.state["resources"]["dashboard"]["id"] == "dashboard"
        assert actual.resources.folder_for("widget") is folder


@pytest.mark.parametrize("problem", ["source", "chart", "name", "type", "level"])
def test_context_rejects_incompatible_installation_before_writes(session_state, problem):
    client = Mock()
    client.capabilities = {
        "dataset_sources": ["CH_TABLE"],
        "chart_factories": {"wizard": ["line"], "ql": [], "editor": []},
    }
    connection = SimpleNamespace(
        id="conn", name="CH", type="clickhouse", raw={"raw_sql_level": "dashsql"}
    )
    client.get.connection.return_value = connection
    if problem == "source":
        client.capabilities["dataset_sources"] = []
    elif problem == "chart":
        client.capabilities["chart_factories"]["wizard"] = []
    elif problem == "name":
        connection.name = "Other"
    elif problem == "type":
        connection.type = "postgres"
    else:
        connection.raw = {}
    options = arguments()
    if problem == "level":
        options["chart_definitions"]["trend"]["family"] = "ql"
        client.capabilities["chart_factories"]["ql"] = ["line"]
    with patch.object(context, "datalens_client", return_value=nullcontext(client)), patch.object(
        context, "ensure_target_folder"
    ) as folder, pytest.raises(DataLensUtilsError), context.dashboard_context(**options):
        pass
    folder.assert_not_called()
