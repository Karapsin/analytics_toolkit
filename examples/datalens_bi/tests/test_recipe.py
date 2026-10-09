"""Local recipe validation with synthetic deployment identity; no cloud calls."""

import json
from pathlib import Path
import tempfile
import unittest

import dashboard
from analytics_toolkit.datalens_utils import DataLensProject, Deployment


class RecipeTests(unittest.TestCase):
    def test_complete_recipe_validates_without_runtime_writes(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary) / "absent-runtime"
            project = DataLensProject(dashboard.ROOT, runtime,
                Deployment("offline-org", "Offline/Dashboards", "Offline dashboard", "Offline connection", "offline-connection", "offline-profile"))
            result = project.validate()
            self.assertEqual(result["request_count"], 0)
            self.assertFalse(runtime.exists())

    def test_independent_sql_contains_the_configured_source(self):
        dataset = json.loads((dashboard.ROOT / "configs/DL objects/datasets.json").read_text())["retail"]
        for path in (dashboard.ROOT / "sql_tests").glob("*.sql"):
            query = path.read_text()
            self.assertIn("FROM " + dataset["table"], query)
            self.assertNotIn("__TABLE_", query)
            self.assertNotIn("__FILTERS__", query)


if __name__ == "__main__":
    unittest.main()
