"""HTML source bounds, metadata revisions and persistence-before-read recovery."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from analytics_toolkit.datalens_utils.bi_html import HTML_MAX_BYTES, html_content, reconcile_html
from analytics_toolkit.datalens_utils.editing.state import fingerprint
from analytics_toolkit.datalens_utils.errors import DataLensConfigurationError, DataLensUtilsError
from analytics_toolkit.datalens_utils.recipe import load_registry
from analytics_toolkit.datalens_utils.resources.bi_store import BIResourceStore
from datalens_sdk import (
    DataLensClientYC,
    EntryLocation,
    HtmlPage,
    HtmlPageCreate,
    HtmlPageUpdate,
    NoAuthProvider,
)

from tests.datalens_utils._support.bi import write


def recipe(project):
    root = project.paths.project_root
    (root / "assets").mkdir()
    (root / "assets/page.html").write_text("<h1>Привет</h1>", encoding="utf-8")
    write(
        root,
        "configs/DL objects/html_pages.json",
        {"page": {"name": "Page", "description": "Authored", "content_file": "assets/page.html"}},
    )
    registry = load_registry()
    store = BIResourceStore(registry)
    store.set_location(SimpleNamespace(key="Offline/Variants/", list_entries=list))
    return registry.resources["html_page:page"], store


def page(**changes):
    value = HtmlPage(
        id="page",
        name="Page",
        installation="yacloud",
        description="Authored",
        location=EntryLocation.path("Offline/Variants"),
        saved_id="r",
        published_id="r",
    )
    return replace(value, **changes)


@pytest.mark.parametrize("problem", ["missing", "encoding", "oversize", "outside"])
def test_html_asset_validation_before_persistence(bi_project, problem):
    resource, _ = recipe(bi_project)
    path = bi_project.paths.project_root / "assets/page.html"
    if problem == "missing":
        resource.definition.pop("content_file")
    elif problem == "encoding":
        path.write_bytes(b"\xff")
    elif problem == "oversize":
        path.write_text("é" * (HTML_MAX_BYTES // 2 + 1))
    else:
        resource.definition["content_file"] = "../outside.html"
    with pytest.raises(DataLensConfigurationError):
        html_content(resource.definition)
    assert not bi_project.paths.runtime_root.exists()


def test_creation_checkpoint_survives_failed_get_and_is_not_repeated(bi_project, monkeypatch):
    resource, store = recipe(bi_project)
    persisted = page(saved_id="draft", published_id=None, warnings=("Processed source",))
    builds = []

    def build(builder):
        builds.append(builder.to_spec())
        return persisted

    monkeypatch.setattr(HtmlPageCreate, "build", build)
    monkeypatch.setattr(
        HtmlPage,
        "publish_revision",
        lambda value, **kwargs: replace(value, published_id=value.saved_id),
    )
    getter = Mock(side_effect=RuntimeError("synthetic read failure"))
    with DataLensClientYC(auth=NoAuthProvider()) as public:
        client = SimpleNamespace(create=public.create, get=SimpleNamespace(html_page=getter))
        with pytest.raises(RuntimeError, match="read failure"):
            reconcile_html(client, store, resource)
        entry = store.state["resources"][resource.key]
        assert entry["id"] == "page"
        assert entry["source_fingerprint"] == fingerprint("<h1>Привет</h1>")
        registry = load_registry()
        resumed = BIResourceStore(registry)
        resumed.set_location(store.folder)
        getter.side_effect = None
        getter.return_value = persisted
        calls = 0

        def read(**kwargs):
            nonlocal calls
            calls += 1
            return persisted if calls == 1 else replace(persisted, published_id="draft")

        getter.side_effect = read
        result = reconcile_html(client, resumed, registry.resources[resource.key])
        assert result.saved_id == result.published_id == "draft"
        assert len(builds) == 1
        assert builds[0].content == "<h1>Привет</h1>"
        assert builds[0].description == "Authored"


def test_changed_source_updates_once_then_is_noop(bi_project, monkeypatch):
    resource, store = recipe(bi_project)
    original = page()
    store.checkpoint(resource.key, original, scope="html_page")
    store.state["resources"][resource.key].update(
        source_fingerprint=fingerprint("old source"), published_id="r"
    )
    current = original
    writes = []

    def execute(update):
        nonlocal current
        writes.append(update.to_spec())
        current = page(saved_id="next", published_id="next")
        return current

    monkeypatch.setattr(HtmlPageUpdate, "execute", execute)
    getter = Mock(side_effect=lambda **kwargs: current)
    with DataLensClientYC(auth=NoAuthProvider()) as public:
        client = SimpleNamespace(create=public.create, get=SimpleNamespace(html_page=getter))
        updated = reconcile_html(client, store, resource)
        store.remember(resource.key, updated)
        assert updated.id == "page"
        assert len(writes) == 1
        assert reconcile_html(client, store, resource).id == "page"
        assert len(writes) == 1


@pytest.mark.parametrize("problem", ["unowned_source", "remote_revision", "verification"])
def test_html_refuses_unmergeable_remote_metadata(bi_project, problem):
    resource, store = recipe(bi_project)
    value = page()
    store.checkpoint(resource.key, value, scope="html_page")
    if problem != "unowned_source":
        store.state["resources"][resource.key].update(
            source_fingerprint=fingerprint(html_content(resource.definition)), published_id="old"
        )
    if problem == "remote_revision":
        store.remember(resource.key, value)
        value = page(saved_id="remote", published_id="remote")
    client = SimpleNamespace(get=SimpleNamespace(html_page=Mock(return_value=value)))
    with pytest.raises(DataLensUtilsError, match=r"baseline|remotely|relationship"):
        reconcile_html(client, store, resource)
