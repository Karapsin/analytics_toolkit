"""Fixed-ID public SDK exports captured before the BI engine changes."""

import json
import sys
import tempfile
from itertools import count
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

from analytics_toolkit.datalens_utils.dashboard_generation.dashboard.create import (
    create_dashboard,
    create_tabs,
)


def financial_outputs(case, destination):
    sequence = count(1)
    with patch("uuid.uuid4", side_effect=lambda: UUID(int=next(sequence))):
        charts = case.build_charts()
        dashboard = create_dashboard(
            context=case.context,
            path=case.backend.folder.key + "Financial report",
            tabs=create_tabs(definitions=case.tabs),
            description="Offline financial report",
            hide_tabs=True,
        )
        dashboard = case.populate(dashboard, charts)
    outputs = {}
    for key, entity in {"dashboard": dashboard, **charts}.items():
        (destination / key).mkdir(parents=True)
        folder = entity.to_file(destination / key)
        for path in folder.rglob("*.json"):
            outputs[key + ".json"] = path.read_bytes()
    for key, dataset in case.datasets.items():
        outputs[key + "_contract.json"] = (
            json.dumps(
                {
                    "id": dataset.id,
                    "definition": case.dataset_definitions[key],
                    "fields": [dict(field.raw) for field in dataset.fields],
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        ).encode("utf-8")
    assert len(outputs) == 15
    return outputs


if __name__ == "__main__":
    from tests.datalens_utils._support.financial import FinancialCase

    case = FinancialCase()
    case.setUp()
    try:
        with tempfile.TemporaryDirectory() as temporary:
            outputs = financial_outputs(case, Path(temporary))
        target = Path(sys.argv[1])
        target.mkdir(parents=True, exist_ok=True)
        for name, content in outputs.items():
            (target / name).write_bytes(content)
    finally:
        case.doCleanups()
