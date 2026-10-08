"""Dataset declarations, source identity, and field changes stay explicit."""

from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from analytics_toolkit.datalens_utils.dashboard_generation.datasets import create
from analytics_toolkit.datalens_utils.errors import DataLensUtilsError

from tests.datalens_utils._support.sdk import configured_datasets


def definition(**changes):
    return {
        "name": "Sales",
        "table": "example.sales",
        "fields": {"region": {"cast": "string"}},
        **changes,
    }


def test_source_table_resolves_recipe_or_context():
    assert (
        create.source_table(SimpleNamespace(source_tables={"sales": "example.sales"}), "sales", {})
        == "example.sales"
    )
    for value in (None, "unqualified"):
        with pytest.raises(DataLensUtilsError, match="source"):
            create.source_table(SimpleNamespace(), "sales", {"table": value})


@pytest.mark.parametrize(
    "change",
    [
        {"source": "unknown"},
        {"fields": {"": {"cast": "string"}}},
        {"fields": {"region": {"title": 1, "cast": "string"}}},
        {"fields": {"region": {"kind": "MEASURE", "cast": "string"}}},
        {"calculations": {"": {"formula": "1"}}},
        {"calculations": {"Total": {"formula": ""}}},
        {"calculations": {"Total": {"formula": 1}}},
        {"fields": {"region": {"title": "Same"}}, "calculations": {"Same": {"formula": "1"}}},
    ],
)
def test_invalid_dataset_declarations_stop_before_sdk(change):
    with pytest.raises(DataLensUtilsError):
        create.validate_definition(SimpleNamespace(), "sales", definition(**change))


def test_named_calculations_and_direct_field_cardinality():
    assert create._calculations({"calculations": [{"name": "Total", "formula": "1"}]}) == {
        "Total": {"formula": "1"}
    }
    for fields in ([], [SimpleNamespace(source="region", calc_mode="direct")] * 2):
        with pytest.raises(DataLensUtilsError, match="Expected one"):
            create._direct_field(SimpleNamespace(fields=fields), "region")


def test_field_changes_include_titles_casts_aggregation_and_formula_conflicts():
    definitions = {
        "sales": definition(
            calculations={"Total": {"formula": "1", "cast": "integer", "kind": "MEASURE"}}
        )
    }
    dataset = configured_datasets(definitions)["sales"]
    desired = definition(
        description="Changed",
        fields={
            "region": {
                "title": "Area",
                "cast": "integer",
                "aggregation": "count",
                "kind": "MEASURE",
            }
        },
        calculations={
            "Total": {"formula": "2", "cast": "float", "kind": "DIMENSION", "aggregation": "sum"},
            "Added": {"formula": "1", "cast": "integer", "kind": "MEASURE"},
        },
        aggregations={"region": "count"},
    )
    issues = create.dataset_issues(dataset, desired)
    assert {
        "region title",
        "region cast",
        "region aggregation",
        "Total formula",
        "Total cast",
        "Total kind",
        "Total aggregation",
        "Added calculation",
        "description",
    }.issubset(issues)
    update = Mock()
    create._configure_fields(update, dataset, desired)
    assert update.update_field.call_args.kwargs["title"] == "Area"
    assert update.update_calculation.call_args.kwargs["formula"] == "2"
    assert update.add_calculation.call_args.kwargs["name"] == "Added"
    assert update.description.call_args.args == ("Changed",)
    with pytest.raises(DataLensUtilsError, match="kind"):
        create._configure_fields(
            update, dataset, definition(fields={"region": {"kind": "MEASURE"}})
        )
    with pytest.raises(DataLensUtilsError, match="non-formula"):
        create._configure_fields(
            update, dataset, definition(calculations={"region": {"formula": "1"}})
        )


@pytest.mark.parametrize(
    ("kind", "existing", "draft", "changed"),
    [
        ("ch_table", False, False, False),
        ("ch_subselect", False, False, False),
        ("ch_table", True, True, False),
        ("ch_table", True, False, True),
    ],
)
def test_dataset_creation_reconciliation_preserves_id(
    session_state, kind, existing, draft, changed
):
    config = definition(source=kind)
    if kind == "ch_subselect":
        path = session_state.paths.project_root / "projection.sql"
        path.write_text("SELECT region FROM __TABLE_SALES__", encoding="utf-8")
        config["projection_file"] = "projection.sql"
    query = "SELECT region FROM example.sales"
    source = SimpleNamespace(
        connection_id="conn",
        source_type="CH_SUBSELECT" if kind == "ch_subselect" else "CH",
        parameters={"db_name": "example", "table_name": "sales", "subsql": query},
    )
    dataset = Mock(
        id="stable",
        sources=[source],
        saved_id="saved" if draft else "published",
        published_id="published",
    )
    resources = Mock()
    resources.existing.return_value = dataset if existing else None
    resources.create.return_value = dataset
    resources.rename.return_value = dataset
    resources.persisted.return_value = dataset
    client = Mock()
    client.get.dataset.return_value = dataset
    context = SimpleNamespace(
        client=client,
        resources=resources,
        connection=SimpleNamespace(id="conn"),
        source_tables={"sales": "example.sales"},
    )
    with patch.object(
        create, "dataset_issues", side_effect=[["title"] if changed else [], []]
    ), patch.object(create, "_configure_fields", return_value=dataset.update):
        result = create.create_datasets(context=context, definitions={"sales": config})
    assert result["sales"].id == "stable"
    if existing:
        resources.create.assert_not_called()
    elif kind == "ch_table":
        client.create.source.return_value.ch_table.assert_called_once_with(
            alias="sales", db_name="example", table_name="sales"
        )
    else:
        client.create.source.return_value.ch_subselect.assert_called_once_with(
            alias="sales", subsql=query
        )


@pytest.mark.parametrize(
    "problem", ["count", "connection", "table", "subselect", "postwrite", "kind"]
)
def test_wrong_source_and_failed_postwrite_verification_are_rejected(session_state, problem):
    config = definition()
    if problem == "subselect":
        config.update(source="ch_subselect", projection_file="projection.sql")
        (session_state.paths.project_root / "projection.sql").write_text(
            "SELECT region FROM example.sales", encoding="utf-8"
        )
    if problem == "kind":
        config["source"] = "unknown"
    source = SimpleNamespace(
        connection_id="other" if problem == "connection" else "conn",
        source_type="CH",
        parameters={
            "db_name": "wrong" if problem == "table" else "example",
            "table_name": "sales",
            "subsql": "wrong",
        },
    )
    dataset = Mock(
        id="stable",
        sources=[] if problem == "count" else [source],
        saved_id="rev",
        published_id="rev",
    )
    resources = Mock()
    resources.existing.return_value = dataset
    resources.rename.return_value = dataset
    context = SimpleNamespace(
        client=Mock(), resources=resources, connection=SimpleNamespace(id="conn")
    )
    with patch.object(create, "validate_definition"), patch.object(
        create, "dataset_issues", side_effect=[[], ["bad title"]]
    ), pytest.raises(DataLensUtilsError):
        create.create_datasets(context=context, definitions={"sales": config})


def test_matching_calculation_requires_no_update():
    config = definition(
        aggregations={"region": "none"},
        calculations={"Total": {"formula": "1", "cast": "integer", "kind": "MEASURE"}},
    )
    dataset = configured_datasets({"sales": config})["sales"]
    assert create.dataset_issues(dataset, config) == []
    update = Mock()
    assert create._configure_fields(update, dataset, config) is update
    assert update.mock_calls == []
