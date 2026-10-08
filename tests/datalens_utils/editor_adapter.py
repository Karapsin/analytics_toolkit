"""Editor source safety and typed create/update contracts; no login or network."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from analytics_toolkit.datalens_utils.dashboard_generation.charts import editor
from analytics_toolkit.datalens_utils.errors import DataLensUtilsError
from datalens_sdk import DataLensClientYC, EditorChart, EntryLocation, StaticYCIAMAuthProvider

from tests.datalens_utils._support.context import ProjectTestCase, project_root


class EditorAdapterTests(ProjectTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.root_patch = project_root(self.root)
        self.root_patch.__enter__()
        self.addCleanup(self.root_patch.__exit__, None, None, None)
        self.requests = []

        def reject_network(request):
            self.requests.append(request)
            message = "Editor adapter construction must not make remote calls"
            raise AssertionError(message)

        self.client = DataLensClientYC(
            auth=StaticYCIAMAuthProvider(org_id="offline", token="offline"),
            transport=httpx.MockTransport(reject_network),
        )
        self.addCleanup(self.client.close)
        self.context = SimpleNamespace(client=self.client)
        self.datasets = {
            "retail": SimpleNamespace(id="public-retail"),
            "customers": SimpleNamespace(id="public-customers"),
            "delivery": SimpleNamespace(id="public-delivery"),
        }
        self.folder = EntryLocation.path("Offline/Charts")

    def source(self, filename, content="module.exports = {};\n"):
        path = self.root / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode("utf-8"))
        return filename

    def definition(self, renderer="gravity_charts"):
        scripts = {}
        for tab in editor.TABS[renderer] - {"meta", "activities"}:
            scripts[tab] = self.source(f"assets/js/{renderer}/{tab}.js")
        return {
            "family": "editor",
            "type": renderer,
            "name": "Offline Editor",
            "title": "Editor example",
            "dataset": "retail",
            "scripts": scripts,
        }

    def chart_from_spec(self, spec, **extra):
        return EditorChart(
            id="offline-editor", wire_type=spec.wire_type, data=dict(spec.tabs), **extra
        )

    def test_every_public_renderer_builds_complete_typed_spec_without_network(self):
        for renderer, wire_type in editor.WIRE_TYPES.items():
            with self.subTest(renderer=renderer):
                definition = self.definition(renderer)
                spec = editor.create_builder(
                    self.context, self.datasets, definition, self.folder
                ).to_spec()
                assert spec.wire_type == wire_type
                assert set(spec.tabs) == set(definition["scripts"]) | {"meta"}
                assert json.loads(spec.tabs["meta"]) == {"links": {"dataset": "public-retail"}}
                chart = self.chart_from_spec(spec)
                assert editor.issues(chart, self.context, self.datasets, definition) == []
        assert self.requests == []

    def test_aliases_resolve_runtime_dataset_ids_and_accept_meta_formatting(self):
        definition = self.definition()
        definition["links"] = {"sales": "retail", "people": "customers"}
        spec = editor.create_builder(self.context, self.datasets, definition, self.folder).to_spec()
        assert json.loads(spec.tabs["meta"])["links"] == {
            "sales": "public-retail",
            "people": "public-customers",
        }
        chart = self.chart_from_spec(spec)
        chart.data["meta"] = '{"links":{"people":"public-customers","sales":"public-retail"}}'
        assert editor.issues(chart, self.context, self.datasets, definition) == []
        assert (
            dict(editor.configure(chart.update, self.context, self.datasets, definition).tab_edits)
            == {}
        )

    def test_unchanged_update_has_no_tab_edits_and_preserves_extra_source(self):
        definition = self.definition()
        spec = editor.create_builder(self.context, self.datasets, definition, self.folder).to_spec()
        chart = self.chart_from_spec(spec)
        chart.data["activities"] = "module.exports = {existingHandler: true};\n"
        update = editor.configure(chart.update, self.context, self.datasets, definition)
        assert dict(update.tab_edits) == {}
        assert chart.data["activities"] == "module.exports = {existingHandler: true};\n"
        assert self.requests == []

    def test_source_update_is_exact_and_replaces_only_changed_tab(self):
        definition = self.definition()
        spec = editor.create_builder(self.context, self.datasets, definition, self.folder).to_spec()
        chart = self.chart_from_spec(spec)
        changed = "module.exports = {series: []};\r\n"
        self.source(definition["scripts"]["prepare"], changed)
        assert editor.issues(chart, self.context, self.datasets, definition) == [
            "prepare tab source"
        ]
        update = editor.configure(chart.update, self.context, self.datasets, definition)
        assert dict(update.tab_edits) == {"prepare": changed}
        assert self.requests == []

    def test_wrong_meta_binding_is_detected_and_repaired_as_one_tab(self):
        definition = self.definition()
        spec = editor.create_builder(self.context, self.datasets, definition, self.folder).to_spec()
        chart = self.chart_from_spec(spec)
        chart.data["meta"] = '{"links":{"dataset":"wrong-dataset"}}'
        assert editor.issues(chart, self.context, self.datasets, definition) == ["Meta links"]
        update = editor.configure(chart.update, self.context, self.datasets, definition)
        assert set(update.tab_edits) == {"meta"}
        assert json.loads(update.tab_edits["meta"])["links"] == {"dataset": "public-retail"}

    def test_explicit_meta_is_validated_and_source_text_is_compared_exactly(self):
        definition = self.definition()
        meta = '{"links": {"dataset": "public-retail"}}\n'
        definition["scripts"]["meta"] = self.source("assets/js/meta.json", meta)
        spec = editor.create_builder(self.context, self.datasets, definition, self.folder).to_spec()
        assert spec.tabs["meta"] == meta
        chart = self.chart_from_spec(spec)
        chart.data["meta"] = json.dumps(json.loads(meta))
        assert editor.issues(chart, self.context, self.datasets, definition) == ["meta tab source"]
        self.source(definition["scripts"]["meta"], '{"links":{"dataset":"wrong-dataset"}}')
        with pytest.raises(DataLensUtilsError, match="configured dataset bindings"):
            editor.create_builder(self.context, self.datasets, definition, self.folder)

    def test_dependency_free_chart_omits_meta(self):
        definition = self.definition("selector")
        definition.pop("dataset")
        spec = editor.create_builder(self.context, self.datasets, definition, self.folder).to_spec()
        assert "meta" not in spec.tabs

    def test_selector_prepare_and_missing_required_tabs_fail_locally(self):
        definition = self.definition("selector")
        definition["scripts"]["prepare"] = self.source("prepare.js")
        with pytest.raises(DataLensUtilsError, match="cannot write tabs"):
            editor.create_builder(self.context, self.datasets, definition, self.folder)
        definition["scripts"].pop("prepare")
        definition["scripts"].pop("controls")
        with pytest.raises(DataLensUtilsError, match="requires explicit sources"):
            editor.create_builder(self.context, self.datasets, definition, self.folder)
        assert self.requests == []

    def test_source_paths_cannot_escape_project_or_refer_to_missing_files(self):
        definition = self.definition()
        for path, message in [
            ("../outside.js", "escapes the project"),
            (str(self.root / "absolute.js"), "project-relative"),
            ("absent.js", "does not exist"),
        ]:
            with self.subTest(path=path):
                definition["scripts"]["prepare"] = path
                with pytest.raises(DataLensUtilsError, match=message):
                    editor.validate_definition(definition)
        with tempfile.TemporaryDirectory() as outside:
            foreign = Path(outside) / "secret.js"
            foreign.write_text("private", encoding="utf-8")
            (self.root / "alias.js").symlink_to(foreign)
            definition["scripts"]["prepare"] = "alias.js"
            with pytest.raises(DataLensUtilsError, match="escapes the project"):
                editor.validate_definition(definition)

    def test_bad_meta_unknown_roles_and_malformed_links_fail_locally(self):
        definition = self.definition()
        definition["scripts"]["meta"] = self.source("meta.json", "module.exports = {};\n")
        with pytest.raises(DataLensUtilsError, match="must contain JSON"):
            editor.validate_definition(definition)
        definition["scripts"].pop("meta")
        definition["links"] = {"sales": "missing"}
        with pytest.raises(DataLensUtilsError, match="unknown dataset"):
            editor.create_builder(self.context, self.datasets, definition, self.folder)
        definition["links"] = None
        with pytest.raises(DataLensUtilsError, match="must map nonempty aliases"):
            editor.validate_definition(definition)
        assert self.requests == []

    def test_renderer_transition_is_rejected_before_tab_edit(self):
        definition = self.definition()
        chart = EditorChart(id="offline-editor", wire_type="table_node", data={})
        with pytest.raises(DataLensUtilsError, match="resource was retained"):
            editor.configure(chart.update, self.context, self.datasets, definition)
        assert dict(chart.update.tab_edits) == {}
        assert self.requests == []

    def test_description_is_optional_and_reconciled_through_public_setter(self):
        definition = self.definition()
        definition["description"] = "Public JavaScript example"
        spec = editor.create_builder(self.context, self.datasets, definition, self.folder).to_spec()
        assert spec.description == definition["description"]
        chart = self.chart_from_spec(spec, raw={"annotation": {"description": "Old description"}})
        assert editor.issues(chart, self.context, self.datasets, definition) == ["description"]
        update = editor.configure(chart.update, self.context, self.datasets, definition)
        assert update.description_value == definition["description"]
        assert dict(update.tab_edits) == {}


if __name__ == "__main__":
    unittest.main()
