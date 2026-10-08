"""Published snapshot exports include dependencies without exporting credentials."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from analytics_toolkit.datalens_utils.dashboard_generation.charts.create import create_charts
from analytics_toolkit.datalens_utils.dashboard_generation.dashboard.create import (
    create_dashboard,
    create_tabs,
)
from analytics_toolkit.datalens_utils.resources.store import ResourceStore
from analytics_toolkit.datalens_utils.settings import (
    read_chart_definitions,
    read_config,
    source_tables,
)
from analytics_toolkit.datalens_utils.validation import checks

from tests.datalens_utils._support.context import ProjectTestCase, recipe_root, runtime_root
from tests.datalens_utils._support.sdk import MemoryDataLens, configured_datasets


class ExportTests(ProjectTestCase):
    def test_dependency_revision_path_and_missing_artifact_invalidate_cached_export(self):
        chart = SimpleNamespace(
            id="company-chart-id", rev_id="chart-before", key="Company/Charts/Trend"
        )
        dashboard = SimpleNamespace(
            id="company-dashboard-id", rev_id="unchanged-dashboard", key="Company/Gallery"
        )

        def export(entity, filename):
            def write(destination):
                path = destination / entity.id
                path.mkdir(parents=True, exist_ok=True)
                (path / filename).write_text(entity.rev_id, encoding="utf-8")
                return path

            return Mock(side_effect=write)

        dashboard.to_file = export(dashboard, "dashboard.json")
        chart.to_file = export(chart, "chart.json")
        with tempfile.TemporaryDirectory() as temporary, runtime_root(Path(temporary)):
            before = checks.export_bundle(dashboard, datasets={}, charts={"trend": chart})
            assert checks.export_bundle(dashboard, datasets={}, charts={"trend": chart}) == before
            assert dashboard.to_file.call_count == 1
            chart.rev_id = "chart-after"
            revised = checks.export_bundle(dashboard, datasets={}, charts={"trend": chart})
            assert before != revised
            assert (before / "charts/company-chart-id/chart.json").read_text(
                encoding="utf-8"
            ) == "chart-before"
            chart.key = "Company/Renamed/Trend"
            moved = checks.export_bundle(dashboard, datasets={}, charts={"trend": chart})
            assert moved != revised
            (moved / "charts/company-chart-id/chart.json").unlink()
            assert checks.export_bundle(dashboard, datasets={}, charts={"trend": chart}) == moved
            assert (moved / "charts/company-chart-id/chart.json").read_text(
                encoding="utf-8"
            ) == "chart-after"

    def test_actual_sdk_exports_every_chart_dataset_and_external_selector_outside_project(self):
        backend = MemoryDataLens()
        self.addCleanup(backend.client.close)
        definitions = read_chart_definitions()
        dataset_definitions = read_config("DL objects/datasets.json")
        datasets = configured_datasets(dataset_definitions)
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary)
            resources = ResourceStore(
                backend.folder, {"company": "offline"}, path=runtime / "resources.json"
            )
            resources.set_folders(
                {"dash": backend.folder, "widget": backend.chart_folder, "dataset": backend.folder}
            )
            context = SimpleNamespace(
                client=backend.client,
                connection=backend.connection,
                folder=backend.folder,
                resources=resources,
                source_tables=source_tables(dataset_definitions),
            )
            charts = create_charts(context=context, datasets=datasets, definitions=definitions)
            dashboard = create_dashboard(
                context=context,
                path=backend.folder.key + "Export gallery",
                tabs=create_tabs(definitions=read_config("UI/tabs.json")),
                description="Offline export",
                hide_tabs=False,
            )
            # Dataset exports require a fetched server response snapshot, not
            # constructor-only field fixtures.
            fetched_datasets = {role: backend.seed_dataset(role) for role in datasets}
            before_writes = len(backend.writes)
            with runtime_root(runtime):
                destination = checks.export_bundle(
                    dashboard, datasets=fetched_datasets, charts=charts
                )
                assert destination.is_relative_to(runtime.resolve())
                assert not destination.is_relative_to(recipe_root())
                manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
                expected = {
                    "dashboard",
                    *(f"dataset:{role}" for role in datasets),
                    *(f"chart:{role}" for role in charts),
                }
                assert set(manifest["resources"]) == expected
                assert len(manifest["files"]) == 1 + len(datasets) + len(charts)
                assert "chart:js_selector" in manifest["resources"]
                assert len(list(destination.rglob("dataset.json"))) == 3
                assert (
                    len(list(destination.rglob("chart.json")))
                    == read_config("coverage.json")["object_count"]
                )
                assert list(destination.rglob("connection.json")) == []
                assert all((destination / filename).is_file() for filename in manifest["files"])
                # Runtime IAM auth never becomes part of an SDK entity snapshot.
                assert "offline-test-token" not in "\n".join(
                    path.read_text(encoding="utf-8") for path in destination.rglob("*.json")
                )
                assert (
                    checks.export_bundle(dashboard, datasets=fetched_datasets, charts=charts)
                    == destination
                )
            assert len(backend.writes) == before_writes


if __name__ == "__main__":
    unittest.main()
