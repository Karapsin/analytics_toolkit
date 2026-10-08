"""Offline QL compilation and reconciliation through public SDK objects."""

import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
from analytics_toolkit.datalens_utils.dashboard_generation.charts import ql
from analytics_toolkit.datalens_utils.errors import DataLensUtilsError
from datalens_sdk import (
    Connection,
    DataLensClientYC,
    EntryLocation,
    QLChart,
    StaticYCIAMAuthProvider,
)

from tests.datalens_utils._support.context import ProjectTestCase, recipe_root


class QLAdapterTests(ProjectTestCase):
    def setUp(self):
        self.requests = []

        def unexpected_request(request):
            self.requests.append(request)
            message = "This test must never access cloud or chart data"
            raise AssertionError(message)

        self.client = DataLensClientYC(
            auth=StaticYCIAMAuthProvider(
                org_id="offline-organization", token="offline-placeholder"
            ),
            transport=httpx.MockTransport(unexpected_request),
        )
        self.context = SimpleNamespace(
            client=self.client,
            connection=Connection(id="offline-connection", type="clickhouse"),
            source_tables={
                "retail": "synthetic.retail",
                "acquisition": "synthetic.customers",
                "customers": "synthetic.customers",
                "delivery": "synthetic.delivery",
            },
        )
        self.folder = EntryLocation.path("Team/Gallery/charts")
        self.definition = next(
            json.loads(path.read_text(encoding="utf-8"))
            for path in (recipe_root() / "configs/DL objects/charts/ql").rglob("*.json")
            if path.stem == "ql_line"
        )

    def tearDown(self):
        self.client.close()
        assert self.requests == []

    def chart(self, definition=None):
        """A clearly offline domain fixture derived from the typed builder spec."""
        definition = definition or self.definition
        spec = ql.create_builder(self.context, {}, definition, self.folder).to_spec()
        return QLChart(
            id="offline-chart",
            name=spec.name,
            data={
                "queryValue": spec.query,
                "connection": {"entryId": spec.connection.id, "type": spec.connection.type},
                "params": [dict(parameter.to_mapping()) for parameter in spec.params],
                "visualization": copy.deepcopy(spec.visualization),
                **copy.deepcopy(spec.extra_data),
            },
            raw={"annotation": {"description": spec.description}},
        )

    def test_all_thirteen_gallery_types_compile_and_compare(self):
        definitions = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in (recipe_root() / "configs/DL objects/charts/ql").rglob("*.json")
        ]
        assert {definition["type"] for definition in definitions} == set(ql.VISUALIZATIONS)
        for definition in definitions:
            with self.subTest(kind=definition["type"]):
                chart = self.chart(definition)
                assert ql.issues(chart, self.context, {}, definition) == []
                assert "__TABLE_" not in chart.query_value

    def test_unchanged_recipe_adds_no_typed_update_edits(self):
        chart = self.chart()
        update = ql.configure(chart.update, self.context, {}, self.definition)
        assert update.query_value is None
        assert update.connection_obj is None
        assert update.params_objs is None
        assert update.description_value is None
        assert update.placeholder_edits == {}
        assert update.data_section_edits == {}
        assert not update.has_data_merge

    def test_actual_saved_metadata_changes_are_detected(self):
        chart = self.chart()
        chart.data["queryValue"] += "\n-- Unexpected saved change"
        chart.data["connection"]["entryId"] = "different-offline-connection"
        chart.data["params"][0]["defaultValue"] = "East"
        y = next(item for item in chart.data["visualization"]["placeholders"] if item["id"] == "y")
        y["items"][0]["cast"] = "string"
        chart.raw["annotation"]["description"] = "Unexpected description"
        assert set(ql.issues(chart, self.context, {}, self.definition)) == {
            "query text",
            "connection reference",
            "parameters",
            "y columns",
            "description",
        }
        update = ql.configure(chart.update, self.context, {}, self.definition)
        assert "synthetic.retail" in update.query_value
        assert update.connection_obj is self.context.connection
        assert update.params_objs[0].default_value == "All"
        assert update.placeholder_edits["y"][0].cast == "integer"
        assert update.description_value == self.definition["description"]

    def test_removing_optional_fields_clears_only_that_placeholder(self):
        chart = self.chart()
        definition = copy.deepcopy(self.definition)
        del definition["fields"]["y2"]
        assert ql.issues(chart, self.context, {}, definition) == ["y2 columns"]
        update = ql.configure(chart.update, self.context, {}, definition)
        assert update.placeholder_edits == {"y2": ()}

    def test_alias_objects_and_documented_settings_are_supported(self):
        definition = copy.deepcopy(self.definition)
        definition["columns"] = {}
        definition["fields"] = {
            "x": [{"name": "Date", "cast": "genericdatetime"}],
            "y": [{"name": "Revenue", "cast": "integer"}],
            "y2": [{"name": "Profit", "cast": "integer"}],
        }
        description = definition.pop("description")
        definition["settings"] = {
            "description": {"text": description},
            "tooltips": {"columns": [{"name": "Profit", "cast": "integer"}]},
        }
        chart = self.chart(definition)
        assert ql.issues(chart, self.context, {}, definition) == []

    def test_unsupported_type_transition_retains_resource(self):
        chart = self.chart()
        definition = {**self.definition, "type": "area"}
        with pytest.raises(DataLensUtilsError, match="resource was retained"):
            ql.validate_change(chart, definition)
        assert chart.id == "offline-chart"

    def test_table_tokens_only_accept_qualified_identifiers(self):
        for table in (
            "retail",
            "synthetic.retail; DROP TABLE other",
            "synthetic.retail -- comment",
        ):
            with self.subTest(table=table):
                context = SimpleNamespace(
                    **{**vars(self.context), "source_tables": {"retail": table}}
                )
                with pytest.raises(DataLensUtilsError, match=r"qualified database\.table"):
                    ql.create_builder(context, {}, self.definition, self.folder)
        context = SimpleNamespace(**{**vars(self.context), "source_tables": {}})
        with pytest.raises(DataLensUtilsError, match="source 'retail'"):
            ql.create_builder(context, {}, self.definition, self.folder)

    def test_missing_alias_and_parameter_references_fail_before_any_write(self):
        definition = copy.deepcopy(self.definition)
        definition["fields"]["y"] = ["Missing Revenue"]
        with pytest.raises(DataLensUtilsError, match="missing explicit SQL output aliases"):
            ql.create_builder(self.context, {}, definition, self.folder)
        definition = copy.deepcopy(self.definition)
        definition["params"][0]["name"] = "unused_region"
        with pytest.raises(DataLensUtilsError, match="parameter references differ"):
            ql.create_builder(self.context, {}, definition, self.folder)
        definition = copy.deepcopy(self.definition)
        definition["params"].append(copy.deepcopy(definition["params"][0]))
        with pytest.raises(DataLensUtilsError, match="unique SQL identifiers"):
            ql.validate_definition(definition)

    def test_opaque_settings_unsupported_slots_and_ql_formats_fail_closed(self):
        variants = [
            {**self.definition, "settings": {"data": {"blob": {}}}},
            {**self.definition, "fields": {**self.definition["fields"], "measures": ["Revenue"]}},
            {**self.definition, "formats": {"Revenue": {"precision": 2}}},
            {**self.definition, "sort": [["Revenue", "desc"]]},
        ]
        for definition in variants:
            with self.subTest(definition=definition), pytest.raises(DataLensUtilsError):
                ql.validate_definition(definition)

    def test_bad_query_paths_and_unresolved_tokens_are_rejected(self):
        for relative in ("/tmp/ql.sql", "../ql.sql", "assets/sql/missing.sql"):
            with self.subTest(relative=relative), pytest.raises(DataLensUtilsError):
                ql.validate_definition({**self.definition, "query_file": relative})
        query = (recipe_root() / self.definition["query_file"]).read_text(encoding="utf-8")
        with patch.object(
            ql, "_read_query", return_value=query.replace("__TABLE_RETAIL__", "__TABLE_retail__")
        ), pytest.raises(DataLensUtilsError, match="unresolved table placeholder"):
            ql.create_builder(self.context, {}, self.definition, self.folder)

    def test_required_and_capacity_constraints_are_checked_locally(self):
        definition = copy.deepcopy(self.definition)
        definition["fields"]["x"] = []
        with pytest.raises(DataLensUtilsError, match="nonempty 'x'"):
            ql.validate_definition(definition)
        definition["fields"]["x"] = ["Date", "Date"]
        with pytest.raises(DataLensUtilsError, match="at most 1"):
            ql.validate_definition(definition)


if __name__ == "__main__":
    unittest.main()
