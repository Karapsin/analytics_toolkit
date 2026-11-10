"""Audit integration scenarios retain a matching, executable fast regression."""

from __future__ import annotations

import ast

from tests._support.paths import REPO_ROOT
from tests.sql.integration.manifest import load_manifest


def test_audit_scenarios_have_existing_fast_regression_companions() -> None:
    scenarios = {
        name: scenario
        for name, scenario in load_manifest()["scenarios"].items()
        if name.startswith("audit.")
    }
    assert scenarios
    for scenario in scenarios.values():
        assert scenario["profile"] == "core"
        references = scenario["regression_tests"]
        assert references
        for reference in references:
            file_name, separator, node = reference.partition("::")
            assert separator, reference
            assert not file_name.startswith("tests/sql/integration/"), reference
            module = ast.parse((REPO_ROOT / file_name).read_text())
            functions = {item.name for item in module.body if isinstance(item, ast.FunctionDef)}
            assert node in functions, reference
