"""Deployment identity, sanitized state, adoption and durable-write recovery."""

import copy
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from analytics_toolkit.datalens_utils import BIProjectDeployment, TargetLocation
from analytics_toolkit.datalens_utils.errors import DataLensUtilsError
from analytics_toolkit.datalens_utils.recipe import Resource, load_registry
from analytics_toolkit.datalens_utils.resources.bi_store import (
    BIResourceStore,
    safe_metadata,
    safe_parameter,
)
from analytics_toolkit.datalens_utils.session import current_session
from datalens_sdk import Connection, EntryLocation, HtmlPage

from tests.datalens_utils._support.bi import dataset


def folder():
    return SimpleNamespace(key="Offline/Variants/", list_entries=list)


def test_pending_save_resumes_exact_saved_branch_without_recreation(bi_project):
    registry = load_registry()
    store = BIResourceStore(registry)
    store.set_location(folder())
    saved = replace(dataset(), saved_id="draft")
    store.checkpoint("dataset:sales", saved, scope="dataset", mutation=True)
    store = BIResourceStore(registry)
    store.set_location(folder())
    getter = Mock(return_value=saved)
    assert store.existing("dataset:sales", "Sales", getter, scope="dataset") is saved
    getter.assert_called_once_with(by_id="sales", branch="saved")
    store.check_write("dataset:sales", saved)
    store.remember("dataset:sales", replace(saved, published_id="draft"))
    assert "pending_write" not in store.state["resources"]["dataset:sales"]
    getter.reset_mock()
    getter.return_value = replace(saved, published_id="draft")
    store.existing("dataset:sales", "Sales", getter, scope="dataset")
    getter.assert_called_once_with(by_id="sales", branch="published")


@pytest.mark.parametrize("problem", ["identity", "scope", "remote", "draft", "configured_id"])
def test_checkpoint_rejects_ownership_conflicts(bi_project, problem):
    registry = load_registry()
    store = BIResourceStore(registry)
    store.set_location(folder())
    value = dataset()
    store.remember("dataset:sales", value)
    if problem == "identity":
        store.state["target"]["target"]["value"] = "Elsewhere"
        store.save_checkpoint()
        with pytest.raises(DataLensUtilsError, match="identity changed"):
            BIResourceStore(registry)
    elif problem == "configured_id":
        registry.resources["dataset:sales"].definition["id"] = "other"
        with pytest.raises(DataLensUtilsError, match="disagree"):
            BIResourceStore(registry)
    elif problem == "scope":
        store.state["resources"]["dataset:sales"]["scope"] = "widget"
        with pytest.raises(DataLensUtilsError, match="kind changed"):
            store.existing("dataset:sales", "Sales", Mock(), scope="dataset")
    else:
        changed = replace(value, saved_id="remote", published_id="remote")
        if problem == "draft":
            store.state["resources"]["dataset:sales"].pop("metadata")
            changed = replace(value, saved_id="unowned")
        with pytest.raises(DataLensUtilsError, match=r"remotely|draft"):
            store.check_write("dataset:sales", changed)


def test_readonly_store_does_not_create_runtime_and_rejects_writes(bi_project):
    store = BIResourceStore(load_registry(), readonly=True)
    with pytest.raises(DataLensUtilsError, match="Read-only"):
        store.save_checkpoint()
    assert not store.path.exists()


def test_saved_pull_baseline_allows_deliberate_draft_edit(bi_project):
    store = BIResourceStore(load_registry())
    draft = replace(dataset(), saved_id="draft")
    entry = store.state["resources"]["dataset:sales"]
    entry.update(branch="saved", metadata=safe_metadata(draft, "dataset"))
    store.check_write("dataset:sales", draft)
    with pytest.raises(DataLensUtilsError, match="remotely"):
        store.check_write("dataset:sales", replace(draft, saved_id="changed"))


def test_adoption_is_exact_unique_and_checks_location(bi_project):
    registry = load_registry()
    registry.resources["dataset:sales"].definition.pop("id")
    store = BIResourceStore(registry)
    store.set_location(folder())
    assert store.existing("dataset:sales", "Sales", Mock(), scope="dataset") is None
    summary = SimpleNamespace(id="sales", scope="dataset", name="Sales")
    store.entries["dataset"] = [summary, summary]
    with pytest.raises(DataLensUtilsError, match="Multiple"):
        store.existing("dataset:sales", "Sales", Mock(), scope="dataset")
    store.entries["dataset"] = [summary]
    assert (
        store.existing("dataset:sales", "Sales", Mock(return_value=dataset()), scope="dataset").id
        == "sales"
    )
    with pytest.raises(DataLensUtilsError, match="outside"):
        store.verify_location(replace(dataset(), location=EntryLocation.path("Wrong")))
    with pytest.raises(DataLensUtilsError, match="name"):
        store.verify_location(dataset(), "Other")


def test_html_adoption_uses_artifact_scope_and_html_type(bi_project):
    registry = load_registry()
    definition = {"name": "Page", "content_file": "assets/page.html"}
    registry.resources["html_page:page"] = Resource(
        "html_page:page", "html_page", definition, {"definition": definition}, ()
    )
    store = BIResourceStore(registry)
    store.set_location(folder())
    store.entries["html_page"] = [
        SimpleNamespace(id="wrong", scope="artifact", type="other", name="Page"),
        SimpleNamespace(id="page", scope="artifact", type="html-page", name="Page"),
    ]
    page = HtmlPage(
        id="page",
        name="Page",
        location=EntryLocation.path("Offline/Variants"),
        saved_id="r",
        published_id="r",
    )
    getter = Mock(return_value=page)
    assert store.existing("html_page:page", "Page", getter, scope="html_page") is page
    getter.assert_called_once_with(by_id="page")


def test_legacy_migration_is_atomic_and_preserves_ownership(bi_project):
    registry = load_registry()
    connection = registry.resources["connection:ch"]
    registry.resources["connection:default"] = replace(connection, key="connection:default")
    old = {
        "version": 1,
        "target": {
            "organization_id": "offline",
            "folder_path": "Offline/Variants",
            "connection_id": "ch",
        },
        "resources": {
            "chart:trend": {
                "id": "trend",
                "scope": "widget",
                "owned_aliases": ["a"],
                "pending_write": {"saved_id": "draft"},
            }
        },
    }
    path = bi_project.paths.runtime_root / "resources.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(old))
    before = path.read_bytes()
    edit = {"resources": {"chart:trend": {"baseline": {"old": "value"}}}}
    (path.parent / "edit-state.json").write_text(json.dumps(edit))
    store = BIResourceStore(registry, readonly=True)
    assert path.read_bytes() == before
    assert store.state["version"] == 2
    assert store.state["resources"]["chart:trend"] == old["resources"]["chart:trend"]
    assert store.state["legacy_edit_state"] == edit
    store.readonly = False
    store.save_checkpoint()
    assert json.loads(path.read_text())["legacy_target"] == old["target"]
    assert not list(path.parent.glob(".resources-*"))
    wrong = copy.deepcopy(old)
    wrong["target"]["connection_id"] = "wrong"
    path.write_text(json.dumps(wrong))
    with pytest.raises(DataLensUtilsError, match="migration refused"):
        BIResourceStore(registry)


def test_workbook_location_uses_workbook_identity(bi_project):
    current_session().deployment = BIProjectDeployment(
        "Dashboard", TargetLocation.workbook(by_id="book"), organization_id="offline"
    )
    store = BIResourceStore(load_registry())
    store.set_location(SimpleNamespace(id="book", collection_id=None, list_entries=list))
    value = replace(dataset(), location=EntryLocation.workbook("book"))
    store.verify_location(value)
    with pytest.raises(DataLensUtilsError, match="outside"):
        store.verify_location(dataset())


def test_connection_checkpoint_redacts_credentials_and_url_secrets(bi_project):
    entity = Connection(
        id="ch",
        type="clickhouse",
        name="CH",
        raw={
            "host": "db",
            "password": "synthetic",
            "unknown": "ignored",
            "url": "https://user:pass@db/query?token=synthetic&region=eu#secret",
        },
    )
    metadata = safe_metadata(entity, "connection")
    assert metadata["parameters"] == {"host": "db", "url": "https://db/query?region=eu"}
    assert safe_parameter("port", 9000) == 9000
    store = BIResourceStore(load_registry())
    store.checkpoint("connection:ch", entity, scope="connection", mutation=True)
    assert "synthetic" not in store.path.read_text()
    assert "pass@" not in store.path.read_text()


def test_wrong_getter_identity_cannot_replace_checkpoint_ownership(bi_project):
    registry = load_registry()
    store = BIResourceStore(registry)
    store.set_location(folder())
    before = copy.deepcopy(store.state)
    with pytest.raises(DataLensUtilsError, match="identity differs"):
        store.existing(
            "dataset:sales",
            "Sales",
            Mock(return_value=replace(dataset(), id="wrong")),
            scope="dataset",
        )
    assert store.state == before
    assert not store.path.exists()


def test_safe_url_preserves_nonsecret_query_encoding():
    url = "https://db/query?region=north%20west&limit=10"
    assert safe_parameter("url", url) == url
