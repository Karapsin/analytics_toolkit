from __future__ import annotations

import inspect
import json
import os
import subprocess
import sys
from pathlib import Path

from analytics_toolkit import sql
from analytics_toolkit.sql.backends.registry import BACKEND_REGISTRY
from analytics_toolkit.sql.orchestration.tasks import _SUPPORTED_TASK_TYPES
from tests.integration.manifest import load_manifest

ROOT = Path(__file__).parents[1]


def test_manifest_v2_classifies_every_public_export_parameter_and_default() -> None:
    manifest = load_manifest()
    exports = manifest["exports"]

    assert set(exports) == set(sql.__all__)
    for name, entry in exports.items():
        assert entry["classification"] in {"integration", "unit_only"}
        obj = getattr(sql, name)
        if not inspect.isfunction(obj):
            assert entry["classification"] == "unit_only"
            assert entry["reason"].strip()
            assert entry["tests"]
            continue
        signature = inspect.signature(obj)
        assert set(entry["parameters"]) == set(signature.parameters), name
        for parameter_name, parameter in signature.parameters.items():
            declared = entry["parameters"][parameter_name]
            expected_default = (
                "<required>"
                if parameter.default is inspect.Parameter.empty
                else repr(parameter.default)
            )
            assert declared["signature_default"] == expected_default, (
                name,
                parameter_name,
            )
            assert declared["kind"] == str(parameter.kind)
            assert declared["states"]
            for state in declared["states"]:
                assert state["status"] in {"covered", "not_applicable"}
                assert state.get("scenarios") or state.get("tests")


def test_manifest_tracks_registry_capabilities_modes_tasks_and_pairs() -> None:
    manifest = load_manifest()
    registered = set(BACKEND_REGISTRY)
    assert set(manifest["backends"]) == registered
    assert set(manifest["backend_capabilities"]) == registered
    for name, adapter in BACKEND_REGISTRY.items():
        declared = manifest["backend_capabilities"][name]
        for field, value in declared.items():
            assert getattr(adapter.capability, field) == value
        assert set(manifest["write_modes"][name]) == set(adapter.supported_write_modes)

    assert set(manifest["task_types"]) == set(_SUPPORTED_TASK_TYPES)
    assert {tuple(pair) for pair in manifest["transfer_pairs"]} == {
        (source, target) for source in registered for target in registered
    }


def test_manifest_references_exact_existing_unit_tests() -> None:
    manifest = load_manifest()
    references: set[str] = set()
    for entry in manifest["exports"].values():
        references.update(entry.get("tests", []))
        for parameter in entry.get("parameters", {}).values():
            for state in parameter["states"]:
                references.update(state.get("tests", []))
    for entry in manifest["unsupported_authentication"].values():
        assert entry["status"] == "not_applicable"
        assert entry["reason"].strip()
        references.update(entry["tests"])

    assert references
    for node_id in references:
        path_text, separator, test_name = node_id.partition("::")
        assert separator, node_id
        assert test_name.startswith("test_"), node_id
        path = ROOT / path_text
        assert path.is_file(), node_id
        assert f"def {test_name.split('[', 1)[0]}(" in path.read_text(encoding="utf-8"), node_id


def test_manifest_scenarios_match_collected_sql_scenarios(tmp_path: Path) -> None:
    manifest = load_manifest()
    environment = os.environ.copy()
    environment["SQL_INTEGRATION_ARTIFACT_DIR"] = str(tmp_path)
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
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    collected = json.loads((tmp_path / "collected-scenarios.json").read_text(encoding="utf-8"))
    collected_by_id = {item["scenario_id"]: item["node_id"] for item in collected}

    assert len(collected_by_id) == len(collected)
    assert set(collected_by_id) == set(manifest["scenarios"])
    for scenario_id, entry in manifest["scenarios"].items():
        assert collected_by_id[scenario_id] == entry["node_id"]


def test_manifest_connection_aliases_cover_required_routes() -> None:
    aliases = set(load_manifest()["connection_aliases"])
    assert {
        "gp",
        "gp_source",
        "gp_target",
        "trino_source_parquet",
        "trino_target_values",
        "trino_source_values",
        "trino_target_parquet",
        "ch_source",
        "ch_target",
        "ch_limited",
        "gp_tls",
        "gp_tls_bundle",
        "gp_tls_client_cert",
        "trino_basic_tls",
        "trino_oauth_tls",
        "ch_tls",
        "ch_tls_variable_ca",
        "airflow_gp",
        "airflow_trino",
        "airflow_ch",
    } <= aliases
