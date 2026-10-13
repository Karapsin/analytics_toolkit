"""Nonconverging saved metadata blocks publication and dataset consumers."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from analytics_toolkit.datalens_utils import bi_engine
from analytics_toolkit.datalens_utils.bi_datasets import reconcile_dataset, verify_dataset_draft
from analytics_toolkit.datalens_utils.errors import DataLensUtilsError
from analytics_toolkit.datalens_utils.resources.bi_store import BIResourceStore
from datalens_sdk import DataLensClientYC, DatasetUpdate, NoAuthProvider

from tests.datalens_utils._support.bi import dataset
from tests.datalens_utils.bi.dataset_lifecycle import setup


def rule(subject):
    return {
        "subject": {"subject_type": "user", "subject_id": subject},
        "allowed_value": "West",
        "pattern_type": "value",
    }


def removal_case(resource, checkpoint, case):
    value = dataset()
    if case == "rls":
        resource.definition["rls"] = {}
        checkpoint["rls_owned"] = {"category": [["user", "owned"]]}
        value = replace(value, rls2={"category": [rule("owned"), rule("unmanaged")]})
    elif case == "filter":
        resource.definition["default_filters"] = []
        checkpoint["default_filters_owned"] = {"category": "owned-filter"}
        value = replace(
            value,
            default_filters=(
                {"id": "owned-filter", "field_guid": "category"},
                {"id": "unmanaged-filter", "field_guid": "amount"},
            ),
        )
    else:
        resource.definition["relations"] = {}
        checkpoint["relations"] = {"owned": "owned-join"}
        value = replace(value, relations=({"id": "owned-join"}, {"id": "unmanaged-join"}))
    return value


def mismatched_case(resource, checkpoint, case):
    if case.startswith("removed_"):
        return removal_case(resource, checkpoint, case[len("removed_") :])
    value = dataset()
    if case == "source":
        resource.definition["sources"]["source"]["parameters"]["table_name"] = "changed"
    elif case == "filter":
        resource.definition["default_filters"] = [
            {"field": "category", "operator": "EQ", "values": ["West"]}
        ]
    elif case == "settings":
        resource.definition["settings"] = {"data_export_forbidden": True}
    elif case == "kind":
        value = replace(
            value,
            result_schema=(
                {**value.result_schema[0], "type": "MEASURE"},
                value.result_schema[1],
            ),
        )
    else:
        resource.definition["sources"]["right"] = dict(resource.definition["sources"]["source"])
        resource.definition["relations"] = {
            "joined": {
                "left": "source",
                "right": "right",
                "type": "left",
                "conditions": [{"left": "category", "right": "category", "operator": "eq"}],
            }
        }
        value = replace(
            value,
            sources=(*value.sources, replace(value.sources[0], id="right-id", title="right")),
            source_avatars=(*value.source_avatars, {"id": "right-avatar", "source_id": "right-id"}),
            relations=(
                {
                    "id": "joined",
                    "left_avatar_id": "avatar",
                    "right_avatar_id": "right-avatar",
                    "join_type": "inner",
                    "required": False,
                    "conditions": [
                        {
                            "left": {"source": "category"},
                            "right": {"source": "category"},
                            "operator": "eq",
                        }
                    ],
                },
            ),
        )
    return value


@pytest.mark.parametrize(
    "case",
    [
        "source",
        "join",
        "filter",
        "settings",
        "kind",
        "removed_rls",
        "removed_join",
        "removed_filter",
    ],
)
def test_nonconvergence_blocks_publication_and_dependents(bi_project, monkeypatch, case):
    registry, store, connection = setup(bi_project)
    resource = registry.resources["dataset:sales"]
    checkpoint = store.state["resources"][resource.key]
    current = mismatched_case(resource, checkpoint, case)
    store.remember(resource.key, current)
    modes = []

    def ignored(builder):
        nonlocal current
        modes.append(builder.to_spec().mode)
        current = replace(current, saved_id="ignored")
        return current

    consumers = Mock()
    monkeypatch.setattr(bi_engine, "create_charts", consumers)
    monkeypatch.setattr(bi_engine, "resolve_location", lambda *args: store.folder)
    monkeypatch.setattr(DatasetUpdate, "execute", ignored)
    with DataLensClientYC(auth=NoAuthProvider()) as public:
        client = SimpleNamespace(
            INSTALLATION=public.INSTALLATION,
            create=public.create,
            capabilities=public.capabilities,
            get=SimpleNamespace(dataset=lambda **kwargs: current),
        )
        with pytest.raises(DataLensUtilsError, match="persisted draft"):
            bi_engine.apply_resources(
                client,
                store,
                registry,
                {resource.key, "chart:trend", "dashboard:main"},
                {resource.key: current, "connection:ch": connection},
            )
    assert modes == ["save"] * 4
    consumers.assert_not_called()
    retained = BIResourceStore(registry).state["resources"][resource.key]
    assert retained["id"] == current.id
    assert "pending_write" in retained
    if case == "removed_rls":
        assert retained["rls_owned"] == {"category": [["user", "owned"]]}
    elif case == "removed_join":
        assert retained["relations"] == {"owned": "owned-join"}
    elif case == "removed_filter":
        assert retained["default_filters_owned"] == {"category": "owned-filter"}


@pytest.mark.parametrize("case", ["rls", "join", "filter"])
def test_interrupted_removals_keep_ownership_until_verified(bi_project, monkeypatch, case):
    registry, store, connection = setup(bi_project)
    resource = registry.resources["dataset:sales"]
    current = removal_case(resource, store.state["resources"][resource.key], case)
    store.remember(resource.key, current)
    modes = []

    def execute(builder):
        nonlocal current
        mode = builder.to_spec().mode
        modes.append(mode)
        if len(modes) == 1:
            # The save returned its ID, but the subsequent metadata fetch is interrupted.
            current = replace(current, saved_id="pending")
        elif mode == "save":
            current = replace(
                current,
                saved_id="removed",
                rls2={"category": [rule("unmanaged")]} if case == "rls" else current.rls2,
                relations=({"id": "unmanaged-join"},) if case == "join" else current.relations,
                default_filters=({"id": "unmanaged-filter", "field_guid": "amount"},)
                if case == "filter"
                else current.default_filters,
            )
        else:
            current = replace(current, published_id=current.saved_id)
        return current

    def fetch(**kwargs):
        if len(modes) == 1:
            message = "interrupted fetch"
            raise RuntimeError(message)
        return current

    monkeypatch.setattr(DatasetUpdate, "execute", execute)
    with DataLensClientYC(auth=NoAuthProvider()) as public:
        getter = Mock(side_effect=fetch)
        client = SimpleNamespace(
            create=public.create,
            capabilities=public.capabilities,
            get=SimpleNamespace(dataset=getter),
        )
        with pytest.raises(RuntimeError, match="interrupted fetch"):
            reconcile_dataset(client, store, resource, {"ch": connection})
        resumed = BIResourceStore(registry)
        resumed.set_location(store.folder)
        owned_key = {
            "rls": "rls_owned",
            "join": "relations",
            "filter": "default_filters_owned",
        }[case]
        assert resumed.state["resources"][resource.key][owned_key]
        getter.side_effect = lambda **kwargs: current
        result = reconcile_dataset(client, resumed, resource, {"ch": connection})
        assert resumed.state["resources"][resource.key][owned_key] == {}
        assert result.saved_id == result.published_id == "removed"
        assert modes == ["save", "save", "publish"]
        resumed.remember(resource.key, result)
        assert reconcile_dataset(client, resumed, resource, {"ch": connection}).id == result.id
        assert modes == ["save", "save", "publish"]


@pytest.mark.parametrize("resume", [False, True])
def test_preflight_saved_selection_is_scoped_and_readonly(bi_project, resume):
    registry, store, connection = setup(bi_project)
    key = "dataset:sales"
    current = replace(dataset(), saved_id="owned-draft")
    store.checkpoint(key, current, scope="dataset", mutation=True)
    checkpoint_before = store.path.read_bytes()
    metadata = Mock(return_value=current)
    client = SimpleNamespace(
        navigation=SimpleNamespace(
            get_entries=lambda **kwargs: [SimpleNamespace(id=current.id, scope="dataset")]
        ),
        get=SimpleNamespace(dataset=metadata, connection=lambda **kwargs: connection),
    )
    result = bi_engine.read_entities(
        client,
        store,
        registry,
        [key, "connection:ch"],
        resume_writes={key} if resume else None,
    )
    metadata.assert_called_once_with(by_id=current.id, branch="saved" if resume else "published")
    assert result[key].id == current.id
    assert store.path.read_bytes() == checkpoint_before


def test_draft_verification_keeps_four_argument_callers(bi_project):
    registry, _, _ = setup(bi_project)
    resource = registry.resources["dataset:sales"]
    current = dataset()
    verify_dataset_draft(current, resource.definition, {"source": current.sources[0]}, resource.key)
