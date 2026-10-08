"""Folder adoption and race recovery remain bounded by the configured path."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from analytics_toolkit.datalens_utils.errors import DataLensUtilsError
from analytics_toolkit.datalens_utils.resources.folders import (
    ensure_resource_folders,
    ensure_target_folder,
)
from datalens_sdk import APIErrorContext, ConflictError, NotFoundError


def error(kind):
    return kind(APIErrorContext(404, "OFFLINE", "offline"))


@pytest.mark.parametrize("path", ["", "Root//Child", "Root/../Child", "Root/./Child"])
def test_invalid_target_path_rejected(path):
    with pytest.raises(DataLensUtilsError, match="TARGET_PATH"):
        ensure_target_folder(client=Mock(), path=path)


def test_root_must_exist_and_returned_path_must_match():
    client = Mock()
    client.get.folder.side_effect = error(NotFoundError)
    with pytest.raises(DataLensUtilsError, match="root"):
        ensure_target_folder(client=client, path="Absent")
    client.get.folder.side_effect = None
    client.get.folder.return_value = SimpleNamespace(key="Other/Folder")
    with pytest.raises(DataLensUtilsError, match="differs"):
        ensure_target_folder(client=client, path="Root/Folder")


@pytest.mark.parametrize("race", [False, True])
def test_missing_descendant_created_or_adopted_after_race(race):
    client = Mock()
    parent = SimpleNamespace(key="Root/")
    child = SimpleNamespace(key="Root/Child/")
    client.get.folder.side_effect = [error(NotFoundError), parent, child]
    if race:
        client.create.folder.return_value.build.side_effect = error(ConflictError)
    assert ensure_target_folder(client=client, path="/Root/Child/") is child
    client.create.folder.assert_called_once_with(name="Child", location=parent)


def test_resource_folders_adopt_existing_and_recover_create_race():
    client = Mock()
    parent = Mock(key="Root/")
    parent.list_entries.return_value = [SimpleNamespace(scope="folder", name="charts/")]
    client.get.folder.side_effect = [
        SimpleNamespace(key="Root/charts/"),
        SimpleNamespace(key="Root/datasets/"),
    ]
    client.create.folder.return_value.build.side_effect = error(ConflictError)
    folders = ensure_resource_folders(
        client=client, parent=parent, names={"charts": "charts", "datasets": "datasets"}
    )
    assert folders["dash"] is parent
    client.create.folder.assert_called_once_with(name="datasets", location=parent)


@pytest.mark.parametrize("problem", ["empty", "dot", "parent", "nested", "duplicate", "outside"])
def test_resource_folder_validation(problem):
    client = Mock()
    parent = Mock(key="Root/")
    names = {"charts": "charts", "datasets": "datasets"}
    parent.list_entries.return_value = []
    client.get.folder.return_value = SimpleNamespace(key="Other/charts/")
    if problem == "duplicate":
        parent.list_entries.return_value = [SimpleNamespace(scope="folder", name="charts")] * 2
    elif problem != "outside":
        names["charts"] = {"empty": "", "dot": ".", "parent": "..", "nested": "a/b"}[problem]
    with pytest.raises(DataLensUtilsError):
        ensure_resource_folders(client=client, parent=parent, names=names)
