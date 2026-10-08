"""Parameter and default-filter updates preserve existing dataset identities."""

from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from analytics_toolkit.datalens_utils.dashboard_generation.datasets import create
from analytics_toolkit.datalens_utils.errors import DataLensUtilsError

from tests.datalens_utils._support.sdk import configured_datasets


@pytest.mark.parametrize("state", ["missing", "changed", "conflict", "matching"])
def test_parameter_configuration_repairs_only_declared_differences(state):
    original = (
        {}
        if state == "missing"
        else {"parameters": {"period": {"type": "string", "default": "month"}}}
    )
    if state == "conflict":
        original = {"fields": {"period": {"cast": "string"}}}
    dataset = configured_datasets({"sales": {"name": "Sales", **original}})["sales"]
    desired = {
        "parameters": {
            "period": {"type": "string", "default": "week" if state == "changed" else "month"}
        }
    }
    update = Mock()
    if state == "conflict":
        assert create.dataset_issues(dataset, desired) == ["period parameter"]
        with pytest.raises(DataLensUtilsError, match="conflicts"):
            create._configure_fields(update, dataset, desired)
    else:
        issues = create.dataset_issues(dataset, desired)
        assert bool(issues) == (state != "matching")
        create._configure_fields(update, dataset, desired)
        if state == "missing":
            update.add_parameter.assert_called_once_with(
                name="period", type="string", default="month"
            )
        elif state == "changed":
            update.update_parameter.assert_called_once_with(
                field=dataset.fields.by_name("period"), type="string", default="week"
            )
        else:
            assert update.mock_calls == []


def test_new_column_requires_unambiguous_owned_source_avatar():
    dataset = SimpleNamespace(fields=[], sources=[SimpleNamespace(id="source")], source_avatars=[])
    with pytest.raises(DataLensUtilsError, match="unambiguous"):
        create._configure_fields(Mock(), dataset, {"fields": {"added": {"cast": "string"}}})


@pytest.mark.parametrize("filter_state", ["missing", "changed", "matching"])
@pytest.mark.parametrize("source_changed", [False, True])
def test_source_and_default_filter_updates_preserve_id(session_state, filter_state, source_changed):
    projection = session_state.paths.project_root / "projection.sql"
    projection.write_text("SELECT region FROM example.sales")
    definition = {
        "name": "Sales",
        "source": "ch_subselect",
        "table": "example.sales",
        "projection_file": "projection.sql",
        "fields": {"region": {"cast": "string"}},
        "default_filters": [{"field": "region", "operator": "EQ", "values": ["North"]}],
    }
    field = SimpleNamespace(guid="region-guid")
    rule = {"column": field.guid, "operation": "EQ", "values": ["North"]}
    source = SimpleNamespace(
        id="source",
        connection_id="connection",
        source_type="CH_SUBSELECT",
        parameters={"subsql": "SELECT previous" if source_changed else projection.read_text()},
    )
    dataset = Mock(
        id="stable",
        sources=[source],
        saved_id="published",
        published_id="published",
        default_filters=[]
        if filter_state == "missing"
        else [
            {
                "id": "filter",
                "field_guid": field.guid,
                "default_filters": [] if filter_state == "changed" else [rule],
            }
        ],
    )
    dataset.fields.by_name.return_value = field
    resources = Mock()
    resources.existing.return_value = resources.rename.return_value = (
        resources.persisted.return_value
    ) = dataset
    context = SimpleNamespace(
        client=Mock(), resources=resources, connection=SimpleNamespace(id="connection")
    )
    with patch.object(create, "dataset_issues", return_value=[]):
        result = create.create_datasets(context=context, definitions={"sales": definition})
    assert result["sales"].id == "stable"
    assert dataset.update.update_source.called == source_changed
    if source_changed:
        dataset.update.update_source.assert_called_once_with(
            source_id="source", parameters={"subsql": projection.read_text()}
        )
    assert dataset.update.add_default_filter.called == (filter_state == "missing")
    assert dataset.update.update_default_filter.called == (filter_state == "changed")
