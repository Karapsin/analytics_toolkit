from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest

from tests._support.paths import REPO_ROOT

ROOT = REPO_ROOT
MANIFEST_PATH = ROOT / "integration/sql_coverage_manifest.json"


@dataclass(frozen=True)
class SqlScenario:
    scenario_id: str
    profile: str
    node_id: str
    backends: tuple[str, ...]


def load_manifest() -> dict[str, Any]:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    _validate_manifest(manifest)
    return manifest


def scenarios() -> dict[str, SqlScenario]:
    records: dict[str, SqlScenario] = {}
    for scenario_id, item in load_manifest()["scenarios"].items():
        records[scenario_id] = SqlScenario(
            scenario_id=scenario_id,
            profile=item["profile"],
            node_id=item["node_id"],
            backends=tuple(item.get("backends", [])),
        )
    return records


def scenario_param(scenario_id: str, *values: object) -> Any:
    record = scenarios().get(scenario_id)
    if record is None:
        msg = f"SQL scenario is absent from the manifest: {scenario_id}"
        raise ValueError(msg)
    return pytest.param(
        *values,
        id=scenario_id,
        marks=pytest.mark.sql_scenario(scenario_id),
    )


def _validate_manifest(manifest: dict[str, Any]) -> None:
    required = {
        "schema_version",
        "backends",
        "backend_capabilities",
        "write_modes",
        "transfer_pairs",
        "connection_aliases",
        "task_types",
        "exports",
        "scenarios",
        "unsupported_authentication",
    }
    missing = sorted(required - manifest.keys())
    if missing:
        msg = f"SQL coverage manifest is missing keys: {missing}"
        raise ValueError(msg)
    if manifest["schema_version"] != 2:
        msg = "SQL coverage manifest schema_version must be 2"
        raise ValueError(msg)
    if not isinstance(manifest["scenarios"], dict) or not manifest["scenarios"]:
        msg = "SQL coverage manifest must declare scenarios"
        raise ValueError(msg)
    for scenario_id, item in manifest["scenarios"].items():
        if not scenario_id or item.get("profile") not in {"core", "auth", "fault", "stress"}:
            msg = f"Invalid SQL scenario record: {scenario_id}"
            raise ValueError(msg)
        if not str(item.get("node_id", "")).startswith("tests/sql/integration/"):
            msg = f"SQL scenario must reference an integration node: {scenario_id}"
            raise ValueError(msg)
