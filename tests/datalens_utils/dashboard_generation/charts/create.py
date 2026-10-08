"""Chart recovery retains durable identity and uses public publish operations."""

from unittest.mock import Mock, patch

import pytest
from analytics_toolkit.datalens_utils.dashboard_generation.charts import create, editor, ql, wizard
from analytics_toolkit.datalens_utils.errors import DataLensUtilsError

from tests.datalens_utils.gallery import GalleryTests


@pytest.mark.parametrize("adapter", [wizard, ql, editor])
def test_publish_uses_family_public_operation(adapter):
    chart = Mock()
    chart.saved_id = "saved-revision"
    create._publish(adapter, chart)
    if adapter is ql:
        chart.update.mode.assert_called_once_with("publish")
    else:
        chart.publish_revision.assert_called_once_with(rev_id="saved-revision")


def test_unknown_family_and_compatibility_adapter():
    with pytest.raises(DataLensUtilsError, match="Unsupported chart family"):
        create.chart_adapter({"family": "unknown"})
    with patch.object(wizard, "configure", return_value="configured") as configure:
        assert create.configure_chart("builder", "dataset", {"dataset": "sales"}) == "configured"
    assert configure.call_args.args[2] == {"sales": "dataset"}
    client = Mock()
    assert create.chart_getter(client, {"family": "wizard"}) is client.get.wizard_chart


def test_checkpoint_family_changes_and_postwrite_mismatch_rejected(session_state):
    gallery = GalleryTests()
    gallery.setUp()
    try:
        definition = gallery.definitions["wizard_line"]
        definitions = {"wizard_line": definition}
        create.create_charts(
            context=gallery.context, datasets=gallery.datasets, definitions=definitions
        )
        checkpoint = gallery.context.resources.state["resources"]["chart:wizard_line"]
        checkpoint["family"] = "ql"
        with pytest.raises(DataLensUtilsError, match="changed family"):
            create.create_charts(
                context=gallery.context, datasets=gallery.datasets, definitions=definitions
            )
        checkpoint["family"] = "wizard"
        with patch.object(
            create, "check_chart", side_effect=[[], ["published drift"]]
        ), pytest.raises(DataLensUtilsError, match="does not match"):
            create.create_charts(
                context=gallery.context, datasets=gallery.datasets, definitions=definitions
            )
    finally:
        gallery.doCleanups()


def test_unchanged_saved_draft_is_published_without_recreation(session_state):
    gallery = GalleryTests()
    gallery.setUp()
    try:
        definitions = {"wizard_line": gallery.definitions["wizard_line"]}
        chart = create.create_charts(
            context=gallery.context, datasets=gallery.datasets, definitions=definitions
        )["wizard_line"]
        saved = chart.update.description(chart.description).mode("save").execute()
        assert saved.saved_id != saved.published_id
        creations = len(
            [
                operation
                for operation, body in gallery.backend.writes
                if operation.startswith("create")
            ]
        )
        result = create.create_charts(
            context=gallery.context, datasets=gallery.datasets, definitions=definitions
        )["wizard_line"]
        assert result.saved_id == result.published_id
        assert result.id == chart.id
        assert (
            len(
                [
                    operation
                    for operation, body in gallery.backend.writes
                    if operation.startswith("create")
                ]
            )
            == creations
        )
    finally:
        gallery.doCleanups()
