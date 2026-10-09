"""Completed drafts, stable identities and recovery around dataset persistence."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from analytics_toolkit.datalens_utils.bi_datasets import reconcile_dataset
from analytics_toolkit.datalens_utils.bi_verification import dataset_issues_v2
from analytics_toolkit.datalens_utils.recipe import load_registry
from analytics_toolkit.datalens_utils.resources.bi_store import BIResourceStore
from datalens_sdk import (
    Connection,
    DataLensClientYC,
    DatasetCreate,
    DatasetUpdate,
    NoAuthProvider,
)

from tests.datalens_utils._support.bi import dataset, write


def setup(project, *, creating=False):
    root = project.paths.project_root
    if creating:
        values = load_registry().resources["dataset:sales"].definition
        values.pop("id")
        write(root, "configs/DL objects/datasets.json", {"sales": values})
    registry = load_registry()
    store = BIResourceStore(registry)
    store.set_location(SimpleNamespace(key="Offline/Variants/", list_entries=list))
    connection = Connection(id="ch", name="CH", type="clickhouse", installation="yacloud")
    return registry, store, connection


def test_creation_publishes_only_complete_draft_and_repeats_without_writes(bi_project, monkeypatch):
    registry, store, connection = setup(bi_project, creating=True)
    resource = registry.resources["dataset:sales"]
    current = replace(dataset(), saved_id="draft", published_id=None)
    built, published, strict_flags = [], [], []

    def build(selected):
        built.append(selected.to_spec())
        return current

    def publish(builder):
        nonlocal current
        assert builder.to_spec().mode == "publish"
        published.append(current.saved_id)
        current = replace(current, published_id=current.saved_id)
        return current

    monkeypatch.setattr(DatasetCreate, "build", build)
    monkeypatch.setattr(DatasetUpdate, "execute", publish)
    with DataLensClientYC(auth=NoAuthProvider()) as public:
        source_type = type(
            public.create.source(using=connection).ch_table(
                alias="source", db_name="example", table_name="sales"
            )
        )
        monkeypatch.setattr(
            source_type,
            "build",
            lambda selected, *, strict: strict_flags.append(strict) or current.sources[0],
        )
        getter = Mock(side_effect=lambda **kwargs: current)
        client = SimpleNamespace(
            create=public.create,
            capabilities=public.capabilities,
            get=SimpleNamespace(dataset=getter),
        )
        result = reconcile_dataset(client, store, resource, {"ch": connection})
        assert published == ["draft"]
        assert strict_flags == [True]
        assert result.saved_id == result.published_id
        assert built[0].actions[0]["field"]["guid"] == "category"
        assert built[0].actions[1]["field"]["guid"] == "amount"
        assert not dataset_issues_v2(
            client,
            result,
            resource.definition,
            store.state["resources"][resource.key],
            {"ch": connection},
        )
        store.remember(resource.key, result)
        assert reconcile_dataset(client, store, resource, {"ch": connection}).id == "sales"
        assert len(built) == 1
        assert published == ["draft"]


def test_interrupted_creation_recovers_owned_draft_without_second_create(bi_project, monkeypatch):
    registry, store, connection = setup(bi_project, creating=True)
    current = replace(dataset(), saved_id="draft", published_id=None)
    builds = []
    monkeypatch.setattr(
        DatasetCreate, "build", lambda selected: builds.append(selected.to_spec()) or current
    )
    monkeypatch.setattr(
        DatasetUpdate, "execute", lambda builder: replace(current, published_id=current.saved_id)
    )
    with DataLensClientYC(auth=NoAuthProvider()) as public:
        source_type = type(
            public.create.source(using=connection).ch_table(
                alias="source", db_name="example", table_name="sales"
            )
        )
        monkeypatch.setattr(source_type, "build", lambda selected, *, strict: current.sources[0])
        getter = Mock(side_effect=RuntimeError("read failed"))
        client = SimpleNamespace(
            create=public.create,
            capabilities=public.capabilities,
            get=SimpleNamespace(dataset=getter),
        )
        with pytest.raises(RuntimeError, match="read failed"):
            reconcile_dataset(
                client, store, registry.resources["dataset:sales"], {"ch": connection}
            )
        assert store.state["resources"]["dataset:sales"]["id"] == "sales"
        resumed = BIResourceStore(registry)
        resumed.set_location(store.folder)
        getter.side_effect = [current, replace(current, published_id="draft")]
        result = reconcile_dataset(
            client, resumed, registry.resources["dataset:sales"], {"ch": connection}
        )
        assert result.published_id == "draft"
        assert len(builds) == 1


def test_formula_update_is_saved_verified_then_published_once(bi_project, monkeypatch):
    registry, store, connection = setup(bi_project)
    resource = registry.resources["dataset:sales"]
    original = dataset()
    store.remember(resource.key, original)
    resource.definition["calculations"]["Amount"]["formula"] = "SUM(2)"
    current = original
    updates, publications = [], []

    def execute(builder):
        nonlocal current
        spec = builder.to_spec()
        if spec.mode == "publish":
            publications.append(current.saved_id)
            assert current.fields.by_name("Amount").formula == "SUM(2)"
            current = replace(current, published_id=current.saved_id)
        else:
            updates.append(spec)
            current = replace(
                original,
                saved_id="next",
                result_schema=(
                    original.result_schema[0],
                    {**original.result_schema[1], "formula": "SUM(2)"},
                ),
            )
        return current

    monkeypatch.setattr(DatasetUpdate, "execute", execute)
    with DataLensClientYC(auth=NoAuthProvider()) as public:
        client = SimpleNamespace(
            create=public.create,
            capabilities=public.capabilities,
            get=SimpleNamespace(dataset=Mock(side_effect=lambda **kwargs: current)),
        )
        updated = reconcile_dataset(client, store, resource, {"ch": connection})
        assert updated.id == original.id
        assert len(updates) == 1
        assert updates[0].mode == "save"
        assert publications == ["next"]
        store.remember(resource.key, updated)
        assert reconcile_dataset(client, store, resource, {"ch": connection}).id == original.id
        assert len(updates) == 1


def test_mismatched_saved_fields_are_not_published(bi_project, monkeypatch):
    registry, store, connection = setup(bi_project)
    resource = registry.resources["dataset:sales"]
    value = dataset()
    store.remember(resource.key, value)
    resource.definition["fields"]["category"]["title"] = "Expected"
    monkeypatch.setattr(DatasetUpdate, "execute", lambda builder: replace(value, saved_id="next"))
    modes = []

    def wrong_update(builder):
        modes.append(builder.to_spec().mode)
        return replace(value, saved_id="next")

    monkeypatch.setattr(DatasetUpdate, "execute", wrong_update)
    getter = Mock(
        side_effect=[value, replace(value, saved_id="next"), replace(value, saved_id="next")]
    )
    with DataLensClientYC(auth=NoAuthProvider()) as public:
        client = SimpleNamespace(
            create=public.create,
            capabilities=public.capabilities,
            get=SimpleNamespace(dataset=getter),
        )
        with pytest.raises(Exception, match="persisted draft"):
            reconcile_dataset(client, store, resource, {"ch": connection})
    assert "publish" not in modes
    assert store.state["resources"][resource.key]["pending_write"]["metadata"]["saved_id"] == "next"
