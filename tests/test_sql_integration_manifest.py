from __future__ import annotations

import inspect
import json
import subprocess
import sys
from pathlib import Path

from analytics_toolkit import sql
from analytics_toolkit.sql.backends.registry import BACKEND_REGISTRY

ROOT = Path(__file__).parents[1]
MANIFEST_PATH = ROOT / "integration/sql_coverage_manifest.json"


def _manifest() -> dict[str, object]:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def test_manifest_classifies_every_public_sql_export_and_parameter() -> None:
    manifest = _manifest()
    integration = manifest["integration_exports"]
    unit_only = manifest["unit_only_exports"]
    classified = set(integration) | set(unit_only)

    assert classified == set(sql.__all__)
    assert set(integration).isdisjoint(unit_only)
    for name in integration:
        signature = inspect.signature(getattr(sql, name))
        assert all(parameter.name for parameter in signature.parameters.values())
    for entry in unit_only.values():
        assert entry["reason"].strip()
        assert (ROOT / entry["test"]).exists()


def test_manifest_tracks_registered_backends_write_modes_and_pairs() -> None:
    manifest = _manifest()
    backends = set(BACKEND_REGISTRY)
    write_modes = {
        mode for adapter in BACKEND_REGISTRY.values() for mode in adapter.supported_write_modes
    }
    expected_pairs = {(source, target) for source in backends for target in backends}

    assert set(manifest["backends"]) == backends
    assert set(manifest["write_modes"]) == write_modes
    assert {tuple(pair) for pair in manifest["transfer_pairs"]} == expected_pairs


def test_manifest_integration_references_are_collected() -> None:
    manifest = _manifest()
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-m",
            "integration",
            "tests/integration",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    collected = completed.stdout
    for node_id in set(manifest["integration_exports"].values()):
        assert node_id in collected
