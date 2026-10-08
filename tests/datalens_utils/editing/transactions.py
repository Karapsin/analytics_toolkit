"""Apply and pull transactions validate ownership before files or resources change."""

from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from analytics_toolkit.datalens_utils.editing import commands
from analytics_toolkit.datalens_utils.errors import DataLensUtilsError


def test_conflicting_shared_assets_abort_staging():
    proposed = {"shared.js": "first"}
    with pytest.raises(DataLensUtilsError, match="shared file"):
        commands.stage_files(proposed, {"shared.js": "second"}, {}, {}, reject_conflicts=True)
    assert proposed == {"shared.js": "first"}


def test_pull_stages_dataset_and_chart_then_saves_distinct_baselines(session_state):
    units = {
        "dashboard": {"files": {"chart_titles": {}}},
        "dataset:sales": {
            "files": {"configs/DL objects/datasets.json": {"sales": {"name": "Sales"}}}
        },
        "chart:trend": {"files": {"chart.json": {"family": "wizard"}, "query.sql": "base"}},
    }
    entities = {key: SimpleNamespace(id=key) for key in units}
    state = Mock(value={"resources": {}})
    with patch.object(
        commands, "fetch", side_effect=lambda client, key, *args, **kwargs: entities[key]
    ), patch.object(commands, "pull_dataset", return_value={"name": "Changed"}), patch.object(
        commands,
        "pull_chart",
        return_value=({"family": "wizard", "title": "Changed"}, {"query.sql": "remote"}),
    ), patch.object(commands, "pull_ui", return_value={"chart_titles": {}}), patch.object(
        commands, "validate_import"
    ) as verify, patch.object(commands, "write_files") as write, patch.object(
        commands, "configuration", return_value=units
    ):
        commands.import_changes(
            SimpleNamespace(client=Mock()),
            SimpleNamespace(branch="saved"),
            set(units),
            units,
            state,
            {"sales": {}},
            {"trend": {}},
        )
    assert verify.call_count == 2
    assert write.call_args.args[0]["query.sql"] == "remote"
    assert state.remember.call_count == 3
    assert all(call.kwargs["branch"] == "saved" for call in state.remember.call_args_list)
    state.save.assert_called_once()


def test_untracked_recipe_is_a_semantic_change():
    assert commands.semantic_change({"files": {}}, {})


def test_import_supports_recipe_without_assets_directory(session_state):
    import shutil  # noqa: PLC0415

    shutil.rmtree(session_state.paths.project_root / "assets")
    with patch.object(commands, "read_config", return_value={}), patch.object(
        commands, "read_chart_definitions", return_value={}
    ), patch.object(commands, "read_contents", return_value={}), patch.object(
        commands, "validate_recipe"
    ):
        commands.validate_import({}, SimpleNamespace(entries={}), {}, {})


@pytest.mark.parametrize("scenario", ["dataset", "ui", "chart", "locked", "dependency", "race"])
def test_apply_validates_dependencies_and_checkpoints_affected_objects(session_state, scenario):
    units = {key: {} for key in ("dataset:sales", "chart:trend", "dashboard")}
    entries = {
        key: SimpleNamespace(saved_id="rev", published_id="rev", is_locked=False) for key in units
    }
    entities = {key: SimpleNamespace(saved_id="rev", rev_id="rev") for key in units}
    selected = (
        {"dataset:sales"}
        if scenario == "dataset"
        else {"dashboard"}
        if scenario == "ui"
        else {"chart:trend"}
    )
    changes = dict.fromkeys(units, "local")
    definitions = {"trend": {"family": "wizard", "dataset": "sales", "title": "Trend"}}
    state = Mock(
        value={"resources": {"dashboard": {"files": {"chart_titles": {"trend": "Trend"}}}}}
    )
    state.owns_write.return_value = False
    if scenario == "locked":
        entries["chart:trend"].is_locked = True
    if scenario == "dependency":
        changes["dataset:sales"] = "remote"
    final = dict(entries)
    if scenario == "race":
        final["chart:trend"] = None
    context = Mock()
    context.resources.record_mutations.return_value = nullcontext()
    with patch.object(commands, "context_for", return_value=context), patch.object(
        commands, "fetch", side_effect=lambda client, key, *args, **kwargs: entities[key]
    ), patch.object(
        commands, "create_datasets", return_value={"sales": entities["dataset:sales"]}
    ) as datasets, patch.object(
        commands,
        "create_charts",
        return_value={"trend": entities["chart:trend"]} if scenario != "ui" else {},
    ), patch.object(
        commands, "populate_dashboard", return_value=entities["dashboard"]
    ) as dashboard, patch.object(commands, "check_queries") as queries, patch.object(
        commands, "inventory", return_value=final
    ), patch.object(commands, "semantic_change", return_value=True):
        if scenario in {"locked", "dependency", "race"}:
            with pytest.raises(
                DataLensUtilsError,
                match={"locked": "locked", "dependency": "Dependency", "race": "verification"}[
                    scenario
                ],
            ):
                commands.apply_changes(
                    Mock(),
                    {},
                    entries,
                    selected,
                    changes,
                    units,
                    state,
                    {"sales": {}},
                    definitions,
                    {},
                )
        else:
            commands.apply_changes(
                Mock(), {}, entries, selected, changes, units, state, {"sales": {}}, definitions, {}
            )
    if scenario in {"locked", "dependency"}:
        state.begin_apply.assert_not_called()
    if scenario == "dataset":
        datasets.assert_called_once()
        dashboard.assert_called_once()
        assert state.remember.call_count == 3
    if scenario == "ui":
        assert queries.call_args.args[-1] == set()
    if scenario == "race":
        state.remember.assert_not_called()
