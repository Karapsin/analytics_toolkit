"""Local version-2 recipe fixture; no external services."""

import json

import pytest
from analytics_toolkit.datalens_utils import BIProjectDeployment, DataLensProject, TargetLocation


def write(root, name, value):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


@pytest.fixture
def bi_project(tmp_path):
    root = tmp_path / "recipe"
    deployment = BIProjectDeployment(
        "Dashboard", TargetLocation.path("Offline/Variants"), organization_id="offline"
    )
    project = DataLensProject(root, tmp_path / "runtime", deployment)
    files = {
        "configs/runtime.json": {"schema_version": 2, "request_interval_seconds": 0},
        "configs/DL objects/connections.json": {
            "ch": {"mode": "existing", "name": "CH", "connector": "clickhouse", "id": "ch"}
        },
        "configs/DL objects/datasets.json": {
            "sales": {
                "name": "Sales",
                "id": "sales",
                "sources": {
                    "source": {
                        "connection": "ch",
                        "factory": "ch_table",
                        "parameters": {"db_name": "example", "table_name": "sales"},
                    }
                },
                "fields": {
                    "category": {
                        "source": "source",
                        "column": "category",
                        "guid": "category",
                        "cast": "string",
                    }
                },
                "calculations": {
                    "Amount": {
                        "formula": "SUM(1)",
                        "kind": "MEASURE",
                        "cast": "float",
                        "guid": "amount",
                    }
                },
            }
        },
        "configs/DL objects/dashboard.json": {"id": "dashboard"},
        "configs/DL objects/charts/wizard/line/trend.json": {
            "key": "trend",
            "id": "trend",
            "family": "wizard",
            "type": "line",
            "name": "Trend",
            "title": "Trend",
            "dataset": "sales",
            "tab": "main",
            "fields": {"x": ["category"], "y": ["Amount"]},
        },
        "configs/UI/tabs.json": {"main": {"title": "Main"}},
        "configs/UI/layout.json": {"main": {"trend": [0, 0, 12, 6]}},
        "configs/UI/titles.json": {},
        "configs/UI/texts.json": {},
        "configs/UI/links/connections.json": {},
        "configs/UI/links/aliases.json": {},
        "configs/UI/links/chart_params.json": {},
        "configs/UI/selectors/selector_groups.json": {},
    }
    for name, value in files.items():
        write(root, name, value)
    with project.session():
        yield project
