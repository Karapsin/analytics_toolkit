"""Backend inference and persisted component errors must not cause duplicate creates."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from analytics_toolkit.datalens_utils import DataLensComponentError
from analytics_toolkit.datalens_utils.bi_datasets import cache_matches, reconcile_dataset
from analytics_toolkit.datalens_utils.bi_preflight import operation_blockers
from analytics_toolkit.datalens_utils.bi_pull import extract_dataset
from analytics_toolkit.datalens_utils.bi_verification import dataset_issues_v2
from analytics_toolkit.datalens_utils.dataset_identity import resolved_definition, resolved_guid
from analytics_toolkit.datalens_utils.errors import DataLensUtilsError
from datalens_sdk import (
    CacheInvalidationSource,
    DataLensClientYC,
    DatasetCreate,
    DatasetUpdate,
    NoAuthProvider,
)

from tests.datalens_utils._support.bi import dataset
from tests.datalens_utils.bi.dataset_lifecycle import setup


def install_inference(public, monkeypatch, current):
    source_type = type(
        public.create.source(
            using=SimpleNamespace(id="ch", type="clickhouse", installation="yacloud")
        ).ch_table(alias="source", db_name="example", table_name="sales")
    )
    monkeypatch.setattr(source_type, "build", lambda selected, *, strict: current.sources[0])


def test_eight_inferred_fields_are_owned_updated_and_not_added_twice(bi_project, monkeypatch):
    registry, store, connection = setup(bi_project, creating=True)
    resource = registry.resources["dataset:sales"]
    fields = {
        "field" + str(index): {
            "source": "source",
            "column": "column" + str(index),
            "guid": "recipe" + str(index),
            "cast": "string",
        }
        for index in range(8)
    }
    fields["field0"]["ui_settings"] = '{"numberFormatting": {"precision": 2}}'
    resource.definition["fields"] = fields
    resource.definition.pop("calculations")
    schema = tuple(
        {
            "guid": "inferred" + str(index),
            "title": "column" + str(index),
            "source": "column" + str(index),
            "avatar_id": "avatar",
            "calc_mode": "direct",
            "cast": "string",
            "type": "DIMENSION",
            "aggregation": "none",
        }
        for index in range(8)
    )
    current = replace(dataset(), saved_id="draft", published_id=None, result_schema=schema)
    builds, writes = [], []
    monkeypatch.setattr(
        DatasetCreate, "build", lambda selected: builds.append(selected.to_spec()) or current
    )

    def execute(builder):
        nonlocal current
        writes.append(builder.to_spec())
        assert not any(value["action"] == "add_field" for value in writes[-1].actions)
        spec = writes[-1]
        rows = [dict(row) for row in current.result_schema]
        for action in spec.actions:
            if action["action"] == "update_field":
                for row in rows:
                    if row["guid"] == action["field"]["guid"]:
                        row.update(action["field"])
        current = replace(
            current,
            result_schema=tuple(rows),
            saved_id="configured",
            published_id="configured" if spec.mode == "publish" else None,
        )
        return current

    monkeypatch.setattr(DatasetUpdate, "execute", execute)
    with DataLensClientYC(auth=NoAuthProvider()) as public:
        install_inference(public, monkeypatch, current)
        client = SimpleNamespace(
            create=public.create,
            capabilities=public.capabilities,
            get=SimpleNamespace(dataset=lambda **kwargs: current),
        )
        result = reconcile_dataset(client, store, resource, {"ch": connection})
        owned = store.state["resources"][resource.key]
        assert result.fields.by_guid("inferred0").ui_settings == fields["field0"]["ui_settings"]
        assert len(owned["field_guids"]) == 8
        assert owned["field_guids"]["field0"] == "inferred0"
        assert resolved_guid(result, "recipe0") == "inferred0"
        assert not dataset_issues_v2(client, result, resource.definition, owned, {"ch": connection})
        store.remember(resource.key, result)
        assert reconcile_dataset(client, store, resource, {"ch": connection}).id == result.id
        assert len(builds) == 1
        assert [spec.mode for spec in writes] == ["save", "publish"]
        assert not builds[0].actions
        entities = {resource.key: result, "connection:ch": connection}
        assert not operation_blockers(client, store, registry, {resource.key}, entities)
        pulled = extract_dataset(result, resource, owned)
        assert pulled["fields"]["field0"]["guid"] == "inferred0"


@pytest.mark.parametrize("stage", ["create", "fetch", "update"])
def test_component_errors_preserve_ids_and_stop_before_publication(bi_project, monkeypatch, stage):
    registry, store, connection = setup(bi_project, creating=True)
    resource = registry.resources["dataset:sales"]
    clean = replace(dataset(), saved_id="draft", published_id=None)
    errors = [{"code": "ERR.DS_FIELD_DUPLICATE", "message": "Duplicate title", "id": "category"}]
    bad = replace(clean, raw={"dataset": {"component_errors": {"items": errors}}})
    created = bad if stage == "create" else clean
    monkeypatch.setattr(DatasetCreate, "build", lambda selected: created)
    execute = Mock(return_value=bad)
    monkeypatch.setattr(DatasetUpdate, "execute", lambda builder: execute(builder))  # noqa: PLW0108 - Bind the mocked method.
    if stage == "update":
        resource.definition["fields"]["category"]["title"] = "Renamed"
    with DataLensClientYC(auth=NoAuthProvider()) as public:
        install_inference(public, monkeypatch, clean)
        getter = Mock(return_value=bad if stage == "fetch" else clean)
        client = SimpleNamespace(
            create=public.create,
            capabilities=public.capabilities,
            get=SimpleNamespace(dataset=getter),
        )
        with pytest.raises(DataLensComponentError) as failure:
            reconcile_dataset(client, store, resource, {"ch": connection})
        assert failure.value.diagnostics["component_errors"] == errors
        assert store.state["resources"][resource.key]["id"] == "sales"
        assert store.path.exists()
        if stage != "update":
            execute.assert_not_called()
        else:
            assert execute.call_count == 1
            assert execute.call_args.args[0].to_spec().mode == "save"


def test_ambiguous_inference_is_a_blocker(bi_project, monkeypatch):
    registry, store, connection = setup(bi_project, creating=True)
    registry.resources["dataset:sales"].definition["fields"]["category"]["guid"] = "requested"
    current = replace(
        dataset(),
        published_id=None,
        result_schema=(
            *dataset().result_schema,
            {**dataset().result_schema[0], "guid": "duplicate"},
        ),
    )
    monkeypatch.setattr(DatasetCreate, "build", lambda selected: current)
    with DataLensClientYC(auth=NoAuthProvider()) as public:
        install_inference(public, monkeypatch, current)
        client = SimpleNamespace(
            create=public.create,
            capabilities=public.capabilities,
            get=SimpleNamespace(dataset=lambda **kwargs: current),
        )
        with pytest.raises(DataLensUtilsError, match="exactly one"):
            reconcile_dataset(
                client, store, registry.resources["dataset:sales"], {"ch": connection}
            )
        assert store.state["resources"]["dataset:sales"]["id"] == "sales"


def test_copy_name_conflict_refuses_adoption(bi_project):
    registry, store, _ = setup(bi_project, creating=True)
    registry.resources["dataset:sales"].definition["copy_from"] = "original"
    store.entries["dataset"] = [SimpleNamespace(id="unrelated", name="Sales", scope="dataset")]
    getter = Mock()
    with pytest.raises(DataLensUtilsError, match="adoption refused"):
        store.existing("dataset:sales", "Sales", getter, scope="dataset")
    getter.assert_not_called()


def test_cache_formula_guid_relocation_preserves_authored_formula():
    original = {
        "fields": {"metric": {"guid": "old"}},
        "cache_invalidation": {
            "field": {"formula": {"formula": "MAX([Metric])", "guid_formula": "MAX([old])"}}
        },
    }
    result = resolved_definition(
        original, {"recipe_field_guids": {"metric": "old"}, "field_guids": {"metric": "new"}}
    )
    assert result["cache_invalidation"]["field"]["formula"] == {
        "formula": "MAX([Metric])",
        "guid_formula": "MAX([new])",
    }
    assert original["fields"]["metric"]["guid"] == "old"


def test_disabled_cache_absence_and_explicit_off_are_equivalent():
    value = dataset()
    desired = CacheInvalidationSource(mode="off")
    assert cache_matches(value, desired)
    assert cache_matches(
        replace(value, raw={"dataset": {"cache_invalidation_source": {"mode": "off"}}}), desired
    )
    assert not cache_matches(
        replace(
            value,
            raw={"dataset": {"cache_invalidation_source": {"mode": "sql", "sql": "SELECT 1"}}},
        ),
        desired,
    )


@pytest.mark.parametrize("explicit_clone_guid", [True, False])
def test_distinct_title_clones_share_a_column_without_sharing_identity(
    bi_project, monkeypatch, explicit_clone_guid
):
    registry, store, connection = setup(bi_project, creating=True)
    resource = registry.resources["dataset:sales"]
    alias = {"source": "source", "column": "category", "title": "Category copy", "cast": "string"}
    if explicit_clone_guid:
        alias["guid"] = "copy-guid"
    resource.definition["fields"] = {
        "alias": alias,
        "category": {"source": "source", "column": "category", "guid": "old", "cast": "string"},
    }
    resource.definition.pop("calculations")
    current = replace(
        dataset(), result_schema=(dataset().result_schema[0],), saved_id="draft", published_id=None
    )
    monkeypatch.setattr(DatasetCreate, "build", lambda builder: current)
    added = []

    def execute(builder):
        nonlocal current
        rows = list(current.result_schema)
        for action in builder.to_spec().actions:
            if action["action"] == "add_field":
                added.append(action["field"])
                rows.append({**action["field"], "calc_mode": "direct"})
        current = replace(
            current,
            result_schema=tuple(rows),
            saved_id="configured",
            published_id="configured" if builder.to_spec().mode == "publish" else None,
        )
        return current

    monkeypatch.setattr(DatasetUpdate, "execute", execute)
    with DataLensClientYC(auth=NoAuthProvider()) as public:
        install_inference(public, monkeypatch, current)
        client = SimpleNamespace(
            create=public.create,
            capabilities=public.capabilities,
            get=SimpleNamespace(dataset=lambda **kwargs: current),
        )
        result = reconcile_dataset(client, store, resource, {"ch": connection})
        assert len(result.fields) == 2
        assert len(added) == 1
        assert added[0]["title"] == "Category copy"
        owned = store.state["resources"][resource.key]
        assert owned["field_guids"]["category"] != owned["field_guids"]["alias"]
        store.remember(resource.key, result)
        reconcile_dataset(client, store, resource, {"ch": connection})
        assert len(added) == 1
