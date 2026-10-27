"""Import must preserve every occurrence of a field-wide measure format."""

import copy
from contextlib import nullcontext

import pytest
from analytics_toolkit.datalens_utils.bi_import_resources import chart_definition
from analytics_toolkit.datalens_utils.import_fidelity import wizard_source_issues

from tests.datalens_utils._support.sdk import MemoryDataLens
from tests.datalens_utils.bi.copy_fidelity import audit_tab, clear_destination


def set_formats(chart, formatting, reverse):
    visualization = chart.data["visualization"]
    original = visualization["y"]["items"][0]
    duplicate = copy.deepcopy(original)
    visualization["y2"] = {"items": [duplicate]}
    first, second = (duplicate, original) if reverse else (original, duplicate)
    first["formatting"] = {"precision": 2, "showRankDelimiter": True}
    second.pop("formatting", None)
    if formatting is not None:
        second["formatting"] = formatting


def file_bytes(root):
    return {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}


@pytest.mark.parametrize("command", ["import_tab", "import_dashboard"])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("formatting", [None, {}, {"precision": 4}])
def test_unrepresentable_occurrence_formats_leave_local_files_untouched(
    bi_project, command, reverse, formatting
):
    api = MemoryDataLens()
    try:
        client, source, charts = audit_tab(api)
        set_formats(next(iter(charts.values())), formatting, reverse)
        clear_destination(bi_project)
        bi_project.client_factory = lambda _: nullcontext(client)
        recipe_before = file_bytes(bi_project.paths.project_root)
        runtime_before = file_bytes(bi_project.paths.runtime_root)
        arguments = {"dashboard_id": source.id, "mode": "copy", "dry_run": False}
        if command == "import_tab":
            arguments["tab_id"] = "audit"
        report = getattr(bi_project, command)(**arguments)
        assert not report["written"]
        assert any("field formats" in item["reason"] for item in report["blockers"])
        assert any(item["status"] == "unsupported" for item in report["fidelity"])
        assert file_bytes(bi_project.paths.project_root) == recipe_before
        after = file_bytes(bi_project.paths.runtime_root)
        assert {
            path: data
            for path, data in after.items()
            if "imports" not in path.relative_to(bi_project.paths.runtime_root).parts
        } == runtime_before
        assert (bi_project.paths.runtime_root / "imports").is_dir()
    finally:
        api.client.close()


@pytest.mark.parametrize("command", ["import_tab", "import_dashboard"])
@pytest.mark.parametrize("reverse", [False, True])
def test_equal_occurrence_formats_import_and_every_occurrence_is_verified(
    bi_project, command, reverse
):
    api = MemoryDataLens()
    try:
        client, source, charts = audit_tab(api)
        chart = next(iter(charts.values()))
        formatting = {"precision": 2, "showRankDelimiter": True}
        set_formats(chart, formatting, reverse)
        clear_destination(bi_project)
        bi_project.client_factory = lambda _: nullcontext(client)
        arguments = {"dashboard_id": source.id, "mode": "copy", "dry_run": False}
        if command == "import_tab":
            arguments["tab_id"] = "audit"
        report = getattr(bi_project, command)(**arguments)
        assert report["written"], report["blockers"]
        ds = client.get.dataset(by_id=chart.dataset_ids[0])
        definition, _ = chart_definition(chart, {"sales": ds}, {}, "one")
        assert definition["formats"]["value1"] == {
            "precision": 2,
            "show_rank_delimiter": True,
        }
        assert not wizard_source_issues(client, chart, {"sales": ds}, definition)
        unformatted = {**definition, "formats": {}}
        assert any(
            "occurrence formatting" in issue
            for issue in wizard_source_issues(client, chart, {"sales": ds}, unformatted)
        )
        chart.data["visualization"]["y2"]["items"][0]["formatting"] = {}
        assert any(
            "occurrence formatting" in issue
            for issue in wizard_source_issues(client, chart, {"sales": ds}, definition)
        )
    finally:
        api.client.close()
