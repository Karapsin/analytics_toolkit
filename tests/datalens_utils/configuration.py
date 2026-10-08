import contextlib
import copy
import importlib.util
import io
import json
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from analytics_toolkit.datalens_utils import settings
from analytics_toolkit.datalens_utils.dashboard_generation.charts import ql
from analytics_toolkit.datalens_utils.dashboard_generation.dashboard import configuration
from analytics_toolkit.datalens_utils.dashboard_generation.datasets import create as dataset_recipe
from analytics_toolkit.datalens_utils.errors import DataLensUtilsError
from analytics_toolkit.datalens_utils.validation import recipe

from tests.datalens_utils._support.context import ProjectTestCase, project_root, recipe_root
from tests.datalens_utils._support.sdk import configured_datasets


class ConfigurationTests(ProjectTestCase):
    def test_gallery_loads_recursively_without_layout_in_chart_definitions(self):
        charts = settings.read_chart_definitions()
        coverage = settings.read_config("coverage.json")
        assert set(charts) == {
            key for family in coverage["families"].values() for key in family["keys"]
        } | {coverage["selector_coverage"]["editor"]["key"]}
        assert all("at" not in definition for definition in charts.values())
        chart_root = recipe_root() / "configs/DL objects/charts"
        for path in chart_root.rglob("*.json"):
            definition = json.loads(path.read_text(encoding="utf-8"))
            relative = path.relative_to(chart_root)
            assert relative.parts[:2] == (definition["family"], definition["type"])
            if definition["type"] == "gravity_charts":
                kind = definition.get("coverage", {}).get("variant") or definition["series_type"]
                assert relative.parts[2] == kind.replace("-", "_")
                assert len(relative.parts) == 4
            elif definition["type"] == "geolayer":
                assert relative.parts[2] == definition["layers"][0]["type"].replace("-", "_")
                assert len(relative.parts) == 4
            else:
                assert len(relative.parts) == 3
        assert set(configuration.read_contents()) == set(coverage["tab_keys"])

    def test_duplicate_semantic_keys_are_rejected_even_in_different_type_directories(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for relative, kind in (
                ("charts/wizard/line/a.json", "line"),
                ("charts/ql/area/b.json", "area"),
            ):
                path = root / "configs/DL objects" / relative
                path.parent.mkdir(parents=True)
                path.write_text(
                    json.dumps(
                        {
                            "key": "same_business_role",
                            "family": relative.split("/")[1],
                            "type": kind,
                            "name": "Name",
                            "title": "Title",
                        }
                    ),
                    encoding="utf-8",
                )
            with project_root(root), pytest.raises(
                DataLensUtilsError, match="Duplicate chart semantic key"
            ):
                settings.read_chart_definitions()

    def mutate_config(self, filename, mutate):
        original = configuration.read_config

        def changed(name):
            value = copy.deepcopy(original(name))
            if name == filename:
                mutate(value)
            return value

        return patch.object(configuration, "read_config", side_effect=changed)

    def test_unknown_missing_overlapping_and_outside_layouts_fail_locally(self):
        mutations = [
            lambda value: value["wizard"].update(unconfigured_widget=[0, 999, 36, 2]),
            lambda value: value["wizard"].pop("wizard_line"),
            lambda value: value["wizard"].update(wizard_line=value["wizard"]["wizard_area"]),
            lambda value: value["wizard"].update(wizard_line=[35, 1000, 2, 3]),
            lambda value: value.update(unknown_tab={}),
        ]
        for mutate in mutations:
            with self.subTest(mutate=mutate), self.mutate_config(
                "UI/layout.json", mutate
            ), pytest.raises(DataLensUtilsError):
                configuration.read_contents()

    def test_manual_selector_requires_parameter_in_each_recipient(self):
        original = configuration.read_chart_definitions

        def invalid_receiver():
            charts = copy.deepcopy(original())
            charts["ql_line"]["params"] = []
            return charts

        with patch.object(
            configuration, "read_chart_definitions", side_effect=invalid_receiver
        ), pytest.raises(DataLensUtilsError, match="not declared"):
            configuration.read_contents()

    def test_selector_connections_cannot_cross_tabs_or_name_unknown_widgets(self):
        for recipient in ("ql_line", "unknown_chart"):
            with self.subTest(recipient=recipient), self.mutate_config(
                "UI/links/connections.json",
                lambda value, recipient=recipient: value["wizard_region_selector"].append(
                    recipient
                ),
            ), pytest.raises(DataLensUtilsError, match="recipient"):
                configuration.read_contents()

    def test_group_membership_and_dataset_aliases_are_checked(self):
        with self.mutate_config(
            "UI/selectors/selector_groups.json",
            lambda value: value["wizard_primary_filters"]["members"].append("unknown_selector"),
        ), pytest.raises(DataLensUtilsError, match="invalid member"):
            configuration.read_contents()
        with self.mutate_config(
            "UI/links/aliases.json",
            lambda value: value["wizard"][0][0].update(field="Unknown Dimension"),
        ), pytest.raises(DataLensUtilsError, match="unknown dataset field"):
            configuration.read_contents()

    def test_company_shareable_json_contains_no_credential_values(self):
        secret_keys = {
            "password",
            "passwd",
            "iam_token",
            "access_token",
            "oauth_token",
            "private_key",
            "client_secret",
        }

        def check(value, path):
            if isinstance(value, dict):
                for key, item in value.items():
                    assert not (key.lower() in secret_keys and item), f"Credential in {path}: {key}"
                    check(item, path)
            elif isinstance(value, list):
                for item in value:
                    check(item, path)

        for path in (recipe_root() / "configs").rglob("*.json"):
            check(json.loads(path.read_text(encoding="utf-8")), path.relative_to(recipe_root()))

    def test_coverage_manifest_rejects_missing_or_duplicate_visual_placement(self):
        datasets, charts = (
            settings.read_config("DL objects/datasets.json"),
            settings.read_chart_definitions(),
        )
        tabs, contents = settings.read_config("UI/tabs.json"), configuration.read_contents()
        assert recipe.validate_recipe(datasets, charts, tabs, contents)["object_count"] == len(
            charts
        )
        for duplicate in (False, True):
            invalid = copy.deepcopy(contents)
            if duplicate:
                invalid["editor"]["charts"]["wizard_line"] = [0, 1000, 36, 10]
            else:
                invalid["wizard"]["charts"].pop("wizard_line")
            with self.subTest(duplicate=duplicate), pytest.raises(
                DataLensUtilsError, match="placed exactly once"
            ):
                recipe.validate_recipe(datasets, charts, tabs, invalid)

    def test_unknown_formula_fields_and_circular_dependencies_fail_preflight(self):
        original = settings.read_config("DL objects/datasets.json")
        charts, tabs, contents = (
            settings.read_chart_definitions(),
            settings.read_config("UI/tabs.json"),
            configuration.read_contents(),
        )
        for formula, message in (
            ("SUM([Unmapped Revenue])", "unknown fields"),
            ("[Revenue] + 1", "circular"),
        ):
            datasets = copy.deepcopy(original)
            datasets["retail"]["calculations"]["Revenue"]["formula"] = formula
            with self.subTest(formula=formula), pytest.raises(DataLensUtilsError, match=message):
                recipe.validate_recipe(datasets, charts, tabs, contents)
        invalid = copy.deepcopy(charts)
        invalid["wizard_line"]["local_fields"][0]["formula"] = "SUM([Unmapped Revenue])"
        with pytest.raises(DataLensUtilsError, match="unknown fields"):
            recipe.validate_recipe(original, invalid, tabs, contents)

    def test_persistent_names_follow_server_grammar_without_restricting_titles(self):
        for name in ("Retail Sales - attempt 4", "План продаж — 2026", "Отчёт_Ёж@company(100%)"):
            with self.subTest(valid_name=name):
                assert recipe.validate_resource_name(name) == name
        for name in ("Retail · Sales", "Dates × Regions", "Charts/Sales", "Trailing.", "", None):  # noqa: RUF001
            with self.subTest(invalid_name=name), pytest.raises(
                DataLensUtilsError, match="resource name"
            ):
                recipe.validate_resource_name(name)
        datasets, charts = (
            settings.read_config("DL objects/datasets.json"),
            settings.read_chart_definitions(),
        )
        charts["wizard_line"]["title"] = "Revenue · date × region"  # noqa: RUF001
        recipe.validate_recipe(
            datasets, charts, settings.read_config("UI/tabs.json"), configuration.read_contents()
        )
        datasets["delivery"]["name"] = "Delivery · Operations"
        with pytest.raises(DataLensUtilsError, match="resource name"):
            recipe.validate_recipe(
                datasets,
                charts,
                settings.read_config("UI/tabs.json"),
                configuration.read_contents(),
            )

    def test_invalid_dashboard_name_stops_entrypoint_before_cloud_context(self):
        spec = importlib.util.spec_from_file_location(
            "offline_invalid_dashboard_name", recipe_root() / "dashboard.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.DASHBOARD_NAME = "Retail · Gallery"
        with patch(
            "analytics_toolkit.datalens_utils.resources.context.dashboard_context"
        ) as context, contextlib.redirect_stderr(io.StringIO()):
            assert module.main() == 1
        context.assert_not_called()

    def test_asset_paths_and_symlink_cannot_leave_shareable_project(self):
        with tempfile.TemporaryDirectory() as temporary, tempfile.TemporaryDirectory() as other:
            root, outside = Path(temporary), Path(other) / "secret.sql"
            outside.write_text("SELECT 1", encoding="utf-8")
            (root / "alias.sql").symlink_to(outside)
            with project_root(root):
                for path in ("../secret.sql", str(outside), "missing.sql", "alias.sql"):
                    with self.subTest(path=path), pytest.raises(DataLensUtilsError):
                        settings.asset_path(path)

    def test_configured_sources_accept_identifiers_and_reject_sql_fragments(self):
        for table in ("unqualified", "db.table; DROP TABLE other", "db.table -- trailing comment"):
            with self.subTest(table=table), pytest.raises(DataLensUtilsError):
                settings.source_tables({"retail": {"table": table}})
        assert settings.source_tables({"retail": {"table": "actual_database.actual_table"}}) == {
            "retail": "actual_database.actual_table"
        }

    def test_runtime_directory_must_be_outside_project(self):
        from analytics_toolkit.datalens_utils import ProjectPaths  # noqa: PLC0415

        with pytest.raises(DataLensUtilsError, match="outside"):
            ProjectPaths(recipe_root(), recipe_root() / ".local")

    def test_dashboard_import_neither_bootstraps_nor_logs_in(self):
        with patch("analytics_toolkit.datalens_utils.bootstrap.ensure_environment") as bootstrap:
            spec = importlib.util.spec_from_file_location(
                "offline_dashboard_entry", recipe_root() / "dashboard.py"
            )
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        bootstrap.assert_not_called()

    def test_validate_command_does_not_open_a_cloud_client(self):
        dashboard = load_dashboard(recipe_root())
        with patch(
            "analytics_toolkit.datalens_utils.auth.client.datalens_client"
        ) as client, contextlib.redirect_stdout(io.StringIO()):
            assert dashboard.main(["validate"]) == 0
        client.assert_not_called()


class ProjectionTests(ProjectTestCase):
    def setUp(self):
        self.definitions = settings.read_config("DL objects/datasets.json")
        self.context = SimpleNamespace(source_tables=settings.source_tables(self.definitions))

    def test_three_explicit_projections_cover_declared_outputs(self):
        assert len(self.definitions) == 3
        for role, definition in self.definitions.items():
            with self.subTest(dataset=role):
                query = dataset_recipe.source_query(self.context, definition)
                assert "__TABLE_" not in query
                assert self.context.source_tables[role] in query
                for field in definition["fields"]:
                    assert re.search(rf"(?i)\bAS\s+`?{field}`?(?:\s|,|$)", query)
        datasets = configured_datasets(self.definitions)
        for role, dataset in datasets.items():
            assert dataset_recipe.dataset_issues(dataset, self.definitions[role]) == []

    def test_projection_rejects_wildcards_missing_tables_and_path_escapes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "projection.sql"
            path.write_text("SELECT * FROM __TABLE_RETAIL__", encoding="utf-8")
            with project_root(root):
                definition = {"projection_file": "projection.sql"}
                with pytest.raises(DataLensUtilsError, match="explicitly name"):
                    dataset_recipe.source_query(self.context, definition)
                path.write_text("SELECT row_id AS row_id FROM __TABLE_RETAIL__", encoding="utf-8")
                with pytest.raises(DataLensUtilsError, match="source table"):
                    dataset_recipe.source_query(SimpleNamespace(source_tables={}), definition)
                for relative in (str(path), "../projection.sql", "absent.sql"):
                    with self.subTest(relative=relative), pytest.raises(DataLensUtilsError):
                        dataset_recipe.source_query(self.context, {"projection_file": relative})

    def test_exact_text_projection_preserves_large_integer_and_decimal_examples(self):
        retail = self.definitions["retail"]
        query = dataset_recipe.source_query(self.context, retail)
        for field in (
            "type_int64",
            "type_int128",
            "type_int256",
            "type_uint64",
            "type_uint128",
            "type_uint256",
            "type_decimal32",
            "type_decimal64",
            "type_decimal128",
            "type_decimal256",
        ):
            with self.subTest(field=field):
                assert retail["fields"][field]["cast"] == "string"
                assert re.search(rf"toString\((?:\w+\.)?`?{field}`?\)\s+AS\s+`?{field}`?", query)
        assert re.search(r"finalizeAggregation\((?:\w+\.)?`?type_aggregatefunction`?\)", query)

    def test_ql_table_binding_preserves_datalens_parameter_syntax(self):
        definition = settings.read_chart_definitions()["ql_line"]
        query = settings.asset_path(definition["query_file"]).read_text(encoding="utf-8")
        # Parameters belong to DataLens; only neutral table tokens are bound by Python.
        from tests.datalens_utils._support.sdk import MemoryDataLens  # noqa: PLC0415

        backend = MemoryDataLens()
        self.addCleanup(backend.client.close)
        context = SimpleNamespace(
            **vars(self.context), client=backend.client, connection=backend.connection
        )
        resolved = ql.create_builder(context, {}, definition, backend.chart_folder).to_spec().query
        import re  # noqa: PLC0415

        assert re.findall(r"\{\{[^}]+\}\}", query) == re.findall(r"\{\{[^}]+\}\}", resolved)

    def test_malformed_projection_tokens_fail_locally(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projection = root / "projection.sql"
            with project_root(root):
                for token in ("__TABLE_retail__", "__TABLE_BAD_ROLE__", "__TABLE_RETAIL"):
                    projection.write_text(f"SELECT row_id AS row_id FROM {token}", encoding="utf-8")
                    with self.subTest(token=token), pytest.raises(DataLensUtilsError):
                        dataset_recipe.source_query(
                            self.context, {"projection_file": "projection.sql"}
                        )

    def test_raw_field_kind_and_aggregate_formula_guards(self):
        for mutate, message in (
            (lambda value: value["fields"]["revenue"].update(kind="MEASURE"), "kind"),
            (
                lambda value: value["calculations"]["Revenue"].update(aggregation="sum"),
                "already aggregates",
            ),
        ):
            definition = copy.deepcopy(self.definitions["retail"])
            mutate(definition)
            with self.subTest(message=message), pytest.raises(DataLensUtilsError, match=message):
                dataset_recipe.validate_definition(self.context, "retail", definition)
        # Complex arithmetic and LOD formulas remain intentional single calculations.
        for role, definition in self.definitions.items():
            dataset_recipe.validate_definition(self.context, role, definition)

    def test_saved_formula_changes_are_detected_independently_of_field_names(self):
        datasets = configured_datasets(self.definitions)
        definitions = copy.deepcopy(self.definitions)
        definitions["retail"]["calculations"]["Revenue"]["formula"] = "SUM([Profit Raw])"
        definitions["retail"]["calculations"]["Margin"]["aggregation"] = "sum"
        assert set(dataset_recipe.dataset_issues(datasets["retail"], definitions["retail"])) == {
            "Revenue formula",
            "Margin aggregation",
        }

    def test_invalid_final_dataset_is_rejected_before_any_remote_operation(self):
        from unittest.mock import Mock  # noqa: PLC0415

        definitions = copy.deepcopy(self.definitions)
        definitions["delivery"]["calculations"]["Deliveries"]["aggregation"] = "sum"
        client, resources = Mock(), Mock()
        context = SimpleNamespace(**vars(self.context), client=client, resources=resources)
        with pytest.raises(DataLensUtilsError, match="already aggregates"):
            dataset_recipe.create_datasets(context=context, definitions=definitions)
        assert client.mock_calls == []
        resources.create.assert_not_called()
        resources.persisted.assert_not_called()

    def test_formula_changes_use_the_typed_dataset_calculation_update(self):
        dataset = configured_datasets(self.definitions)["retail"]
        changed = copy.deepcopy(self.definitions["retail"])
        compound = "SUM([Revenue Raw]) + SUM([Profit Raw]) * (SUM([Orders Raw]))"
        changed["calculations"]["Revenue"]["formula"] = compound
        update = dataset_recipe._configure_fields(dataset.update, dataset, changed).to_spec()
        assert len(update.actions) == 1
        action = update.actions[0]
        assert action["action"] == "update_field"
        assert action["field"] == {
            "guid": dataset.fields.by_name("Revenue").guid,
            "formula": compound,
        }


if __name__ == "__main__":
    unittest.main()


def load_dashboard(root):
    spec = importlib.util.spec_from_file_location("offline_dashboard", root / "dashboard.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
