"""SDK-free identities and explicit unsupported-operation boundaries."""

import ast
import importlib.util
import json
import sys
from importlib.metadata import PackageNotFoundError
from unittest.mock import Mock

import pytest
from analytics_toolkit.datalens_utils import (
    BIProjectDeployment,
    DataLensCapabilityError,
    DataLensConfigurationError,
    DataLensProject,
    TargetLocation,
    capabilities,
    get_capabilities,
)
from analytics_toolkit.datalens_utils.cli import run_cli
from analytics_toolkit.datalens_utils.recipe import Resource, ResourceRegistry

from tests._support.paths import REPO_ROOT


def test_locations_and_installations(tmp_path):
    target = TargetLocation.workbook(key="team")
    deployment = BIProjectDeployment(
        "Sales", target, installation="enterprise", base_url="https://bi.example.test"
    )
    assert deployment.as_dict()["target"] == {
        "kind": "workbook",
        "value": "team",
        "reference": "key",
    }
    assert (
        DataLensProject(tmp_path / "absent", tmp_path / "runtime", deployment).deployment
        is deployment
    )
    assert TargetLocation.path("Users/team/").value == "Users/team"
    assert TargetLocation.workbook(by_id="wb").reference == "id"
    for kwargs in ({}, {"by_id": "wb", "key": "team"}, {"by_id": ""}):
        with pytest.raises(DataLensConfigurationError):
            TargetLocation.workbook(**kwargs)
    for kwargs in (
        {"installation": "other"},
        {"installation": "enterprise"},
        {"installation": "enterprise", "base_url": "https://secret:password@example.test"},
    ):
        with pytest.raises(DataLensConfigurationError):
            BIProjectDeployment("Sales", target, **kwargs)


def test_matrix_without_sdk_and_unknown_version(monkeypatch):
    def missing(_distribution):
        raise PackageNotFoundError

    monkeypatch.setattr(capabilities, "version", missing)
    report = get_capabilities()
    assert report["sdk_version"] is None
    assert report["operations"]["connection.create"]["status"] == "unavailable"
    monkeypatch.setattr(capabilities, "version", lambda _: "9.0.0")
    assert get_capabilities()["sdk_version"] == "9.0.0"
    with pytest.raises(DataLensConfigurationError):
        get_capabilities(installation="invalid")


@pytest.mark.parametrize(
    "operation",
    [
        "report.create",
        "pdf.read",
        "mailing.update",
        "private_embedding.create",
        "html_page.pull",
        "wizard.multi_dataset.create",
        "dataset.avatar.create",
    ],
)
def test_blocked_operations_have_specific_upstream_requirements(operation):
    report = get_capabilities()
    with pytest.raises(DataLensCapabilityError) as caught:
        capabilities.require_capability(report, operation)
    assert caught.value.operation == operation
    assert report["operations"][operation]["requirements"]


@pytest.mark.skipif(
    sys.version_info < (3, 10) or importlib.util.find_spec("datalens_sdk") is None,
    reason="Optional SDK requires Python 3.10+",
)
@pytest.mark.parametrize("installation", ["yc", "enterprise"])
def test_public_inventory_without_network(installation, monkeypatch):
    import httpx  # noqa: PLC0415 - Optional SDK tests are skipped without dependencies.

    monkeypatch.setattr(httpx.Client, "send", Mock(side_effect=AssertionError("Network request")))
    report = get_capabilities(installation=installation)
    assert report["operations"]["dataset.join.create"]["status"] == "available"
    assert "clickhouse" in report["inventories"]["connectors"]
    assert "CH_TABLE" in report["inventories"]["dataset_sources"]


def test_registry_dependency_and_affected_closure():
    resources = {
        key: Resource(key, key.split(":")[0], {}, {}, deps)
        for key, deps in {
            "connection:sales": (),
            "dataset:sales": ("connection:sales",),
            "chart:trend": ("dataset:sales",),
            "dashboard:main": ("chart:trend",),
            "html_page:guide": (),
        }.items()
    }
    registry = ResourceRegistry(resources)
    dependencies, affected = registry.closure(registry.selection(["dataset:sales"]))
    assert dependencies == ["connection:sales", "dataset:sales"]
    assert affected == ["chart:trend", "dashboard:main"]
    with pytest.raises(DataLensConfigurationError):
        registry.selection([])
    with pytest.raises(DataLensConfigurationError):
        registry.selection(["dataset:unknown"])
    with pytest.raises(DataLensConfigurationError, match="cycle"):
        ResourceRegistry({"dataset:a": Resource("dataset:a", "dataset", {}, {}, ("dataset:a",))})
    with pytest.raises(DataLensConfigurationError, match="missing"):
        ResourceRegistry(
            {"dataset:a": Resource("dataset:a", "dataset", {}, {}, ("connection:missing",))}
        )


def test_json_cli_one_result_and_errors(capsys):
    project = Mock()
    project._execute.return_value = {"operation": "plan", "actions": []}
    assert run_cli(project, ["plan", "--resource", "dataset:sales", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == project._execute.return_value
    assert project._execute.call_args.kwargs["resource_keys"] == ["dataset:sales"]
    project._execute.side_effect = DataLensConfigurationError("Invalid")
    assert run_cli(project, ["--json", "validate"]) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "Invalid"


def test_new_base_modules_parse_on_python38():
    for name in ("deployment.py", "capabilities.py", "recipe.py"):
        ast.parse(
            (REPO_ROOT / "analytics_toolkit/datalens_utils" / name).read_text(),
            feature_version=(3, 8),
        )
