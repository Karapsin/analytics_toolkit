# Конфигурация, сериализация и восстановление без облака и доступа к базам.

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from analytics_toolkit.datalens_utils import DataLensProject, Deployment
from analytics_toolkit.datalens_utils.dashboard_generation.charts.create import create_charts
from analytics_toolkit.datalens_utils.dashboard_generation.dashboard import configuration
from analytics_toolkit.datalens_utils.dashboard_generation.dashboard.populate import (
    populate_dashboard,
)
from analytics_toolkit.datalens_utils.resources.store import ResourceStore
from analytics_toolkit.datalens_utils.settings import (
    read_chart_definitions,
    read_config,
    source_tables,
)

from tests._support.paths import REPO_ROOT
from tests.datalens_utils._support.sdk import MemoryDataLens, configured_datasets


class FinancialCase(unittest.TestCase):
    def setUp(self):
        project_temp = tempfile.TemporaryDirectory()
        self.addCleanup(project_temp.cleanup)
        project_root = Path(project_temp.name) / "recipe"
        shutil.copytree(REPO_ROOT / "tests/datalens_utils/_support/financial_recipe", project_root)
        scope = DataLensProject(
            project_root,
            Path(project_temp.name) / "runtime",
            Deployment(
                "offline",
                "Offline/Attempt4",
                "Financial report",
                "Offline ClickHouse",
                "offline-clickhouse",
                "offline",
            ),
        ).session()
        scope.__enter__()
        self.addCleanup(scope.__exit__, None, None, None)
        self.definitions = read_chart_definitions()
        self.dataset_definitions = read_config("DL objects/datasets.json")
        self.datasets = configured_datasets(self.dataset_definitions)
        self.tabs = read_config("UI/tabs.json")
        # Keep publication fixtures valid independently of the user's live layout.
        self.fixture_layout = json.loads(
            (REPO_ROOT / "tests/datalens_utils/_support/fixtures/financial_layout.json").read_text()
        )
        self.fixture_layout.update(
            {key: value for key, value in read_config("UI/layout.json").items() if key != "month"}
        )
        configuration_reads = patch.object(
            configuration,
            "read_config",
            side_effect=lambda name: (
                self.fixture_layout if name == "UI/layout.json" else read_config(name)
            ),
        )
        self.configuration_reads = configuration_reads
        configuration_reads.start()
        self.addCleanup(configuration_reads.stop)
        self.contents = configuration.read_contents()
        self.backend = MemoryDataLens()
        self.addCleanup(self.backend.client.close)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "resources.json"
        self.context = self.new_context()

    def new_context(self):
        tables = source_tables(self.dataset_definitions)
        resources = ResourceStore(
            self.backend.folder,
            {
                "folder_path": self.backend.folder.key.rstrip("/"),
                "organization_id": "offline",
                "connection_id": self.backend.connection.id,
                "source_tables": tables,
            },
            path=self.path,
        )
        resources.set_folders(
            {
                "dash": self.backend.folder,
                "widget": self.backend.chart_folder,
                "dataset": self.backend.folder,
            }
        )
        return SimpleNamespace(
            client=self.backend.client,
            folder=self.backend.folder,
            connection=self.backend.connection,
            resources=resources,
            source_tables=tables,
        )

    def build_charts(self, definitions=None):
        return create_charts(
            context=self.context,
            datasets=self.datasets,
            definitions=self.definitions if definitions is None else definitions,
        )

    def populate(self, dashboard, charts):
        return populate_dashboard(
            context=self.context,
            dashboard=dashboard,
            datasets=self.datasets,
            charts=charts,
            tab_definitions=self.tabs,
            contents=self.contents,
            chart_definitions=self.definitions,
            description="Offline financial report",
            hide_tabs=True,
        )
