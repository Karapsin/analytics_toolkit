"""Public planning inspects interrupted owned drafts without writing."""

from contextlib import nullcontext
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from analytics_toolkit.datalens_utils import bi_engine, planning
from analytics_toolkit.datalens_utils.bi_datasets import reconcile_dataset
from analytics_toolkit.datalens_utils.recipe import ResourceRegistry
from datalens_sdk import DataLensClientYC, DatasetCreate, DatasetUpdate, NoAuthProvider

from tests.datalens_utils._support.bi import dataset
from tests.datalens_utils.bi.dataset_lifecycle import setup
from tests.datalens_utils.bi.inferred_fields import install_inference


@pytest.mark.parametrize("published_state", ["missing", "stale"])
@pytest.mark.parametrize("command", ["plan", "status"])
def test_public_planning_recovers_interrupted_source_only_draft(
    bi_project, monkeypatch, published_state, command
):
    registry, store, connection = setup(bi_project, creating=True)
    resource = registry.resources["dataset:sales"]
    draft = replace(
        dataset(),
        saved_id="source-only",
        published_id=None if published_state == "missing" else "revision",
    )
    builds = Mock(return_value=draft)
    forbidden = Mock(side_effect=AssertionError("planning cannot persist"))
    monkeypatch.setattr(DatasetCreate, "build", builds)
    monkeypatch.setattr(DatasetUpdate, "execute", forbidden)
    with DataLensClientYC(auth=NoAuthProvider()) as public:
        install_inference(public, monkeypatch, draft)
        getter = Mock(side_effect=RuntimeError("interrupted fetch"))
        client = SimpleNamespace(
            INSTALLATION=public.INSTALLATION,
            create=public.create,
            capabilities=public.capabilities,
            navigation=SimpleNamespace(
                get_entries=lambda **kwargs: [SimpleNamespace(id=draft.id, scope="dataset")]
            ),
            get=SimpleNamespace(dataset=getter, connection=lambda **kwargs: connection),
        )
        with pytest.raises(RuntimeError, match="interrupted fetch"):
            reconcile_dataset(client, store, resource, {"ch": connection})
        checkpoint_before = store.path.read_bytes()
        recipe_before = {
            path: path.read_bytes()
            for path in bi_project.paths.project_root.rglob("*")
            if path.is_file()
        }

        def fetch(*, by_id, branch):
            assert by_id == draft.id
            if branch == "published" and published_state == "missing":
                message = "no published revision"
                raise RuntimeError(message)
            return draft if branch == "saved" else dataset()

        getter.side_effect = fetch
        getter.reset_mock()
        lifecycle = ResourceRegistry(
            {key: registry.resources[key] for key in ("connection:ch", resource.key)}
        )
        validate_registry = bi_engine.validate_registry
        monkeypatch.setattr(planning, "load_registry", lambda: lifecycle)
        monkeypatch.setattr(bi_engine, "load_registry", lambda: lifecycle)
        monkeypatch.setattr(
            bi_engine, "validate_registry", lambda selected: validate_registry(registry)
        )
        bi_project.client_factory = lambda _: nullcontext(client)
        report = getattr(bi_project, command)()
        action = next(item for item in report["actions"] if item["resource"] == resource.key)
        assert action["action"] == "recover"
        if command == "status":
            assert report["resource_statuses"][resource.key] == "recover"
        getter.assert_called_once_with(by_id=draft.id, branch="saved")
        assert store.path.read_bytes() == checkpoint_before
        assert {path: path.read_bytes() for path in recipe_before} == recipe_before
        assert builds.call_count == 1
        forbidden.assert_not_called()


def test_scoped_plan_keeps_pending_dataset_dependency_on_published_branch(bi_project, monkeypatch):
    _registry, store, connection = setup(bi_project)
    draft = replace(dataset(), saved_id="pending")
    store.checkpoint("dataset:sales", draft, scope="dataset", mutation=True)
    checkpoint_before = store.path.read_bytes()
    getter = Mock(return_value=dataset())
    with DataLensClientYC(auth=NoAuthProvider()) as public:
        client = SimpleNamespace(
            INSTALLATION=public.INSTALLATION,
            create=public.create,
            capabilities=public.capabilities,
            navigation=SimpleNamespace(
                get_entries=lambda **kwargs: [SimpleNamespace(id=draft.id, scope="dataset")]
            ),
            get=SimpleNamespace(dataset=getter, connection=lambda **kwargs: connection),
        )
        forbidden = Mock(side_effect=AssertionError("planning cannot persist"))
        monkeypatch.setattr(DatasetUpdate, "execute", forbidden)
        monkeypatch.setattr(DatasetCreate, "build", forbidden)
        bi_project.client_factory = lambda _: nullcontext(client)
        report = bi_project.plan(resource_keys=["chart:trend"])
    assert "dataset:sales" in report["read_dependencies"]
    assert "dataset:sales" not in report["write_scope"]
    getter.assert_called_once_with(by_id=draft.id, branch="published")
    assert store.path.read_bytes() == checkpoint_before
    forbidden.assert_not_called()
