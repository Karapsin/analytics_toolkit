"""Verification detects managed metadata differences without querying data."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from analytics_toolkit.datalens_utils.bi_verification import (
    common_resource_issues,
    connection_issues,
    dataset_issues_v2,
    verify_resources,
)
from analytics_toolkit.datalens_utils.errors import DataLensUtilsError
from analytics_toolkit.datalens_utils.recipe import load_registry
from analytics_toolkit.datalens_utils.resources.bi_store import BIResourceStore, entry_description
from datalens_sdk import Connection, DataLensClientYC, EntryLocation, NoAuthProvider

from tests.datalens_utils._support.bi import dataset


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ("title", "fields.category.title"),
        ("source", "sources.source"),
        ("connection", "sources.source"),
        ("settings", "settings.data_export_forbidden"),
        ("filter", "default_filters.0"),
        ("cache", "cache_invalidation"),
        ("rls", "rls.category"),
        ("removed_rls", "rls.removed.category"),
    ],
)
def test_dataset_verification_detects_each_managed_difference(bi_project, change, expected):
    registry = load_registry()
    definition = registry.resources["dataset:sales"].definition
    old = {}
    value = dataset()
    if change == "title":
        definition["fields"]["category"]["title"] = "Renamed"
    elif change == "source":
        definition["sources"]["source"]["parameters"]["table_name"] = "changed"
    elif change == "connection":
        value = replace(value, sources=(replace(value.sources[0], connection_id="foreign"),))
    elif change == "settings":
        definition["settings"] = {"data_export_forbidden": True}
    elif change == "filter":
        definition["default_filters"] = [
            {"field": "category", "operator": "EQ", "values": ["West"]}
        ]
    elif change == "cache":
        definition["cache_invalidation"] = {"mode": "off"}
    elif change == "rls":
        definition["rls"] = {
            "rule": {"field": "category", "subject_id": "reader", "allowed_value": "West"}
        }
    else:
        definition["rls"] = {}
        old = {"rls_owned": {"category": [["user", "reader"]]}}
        value = replace(
            value,
            rls2={
                "category": [
                    {
                        "subject": {"subject_type": "user", "subject_id": "reader"},
                        "allowed_value": "West",
                        "pattern_type": "value",
                    }
                ]
            },
        )
    connection = Connection(id="ch", name="CH", type="clickhouse")
    with DataLensClientYC(auth=NoAuthProvider()) as client:
        issues = dataset_issues_v2(client, value, definition, old, {"ch": connection})
    assert expected in issues


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ("id", "id"),
        ("name", "name"),
        ("description", "description"),
        ("draft", "revisions"),
        ("location", "location.path"),
    ],
)
def test_common_verification_guards_retained_identity_and_metadata(bi_project, change, expected):
    registry = load_registry()
    resource = registry.resources["dataset:sales"]
    store = BIResourceStore(registry, readonly=True)
    value = dataset()
    old = {"id": value.id}
    if change == "id":
        value = replace(value, id="wrong")
    elif change == "name":
        value = replace(value, name="Wrong")
    elif change == "description":
        resource.definition["description"] = "Expected"
    elif change == "draft":
        value = replace(value, saved_id="draft")
    else:
        value = replace(value, location=EntryLocation.path("Elsewhere"))
    assert expected in common_resource_issues(store, resource, value, old)


def test_existing_and_managed_connection_verification_uses_sanitized_metadata():
    value = Connection(id="ch", name="CH", type="clickhouse", raw={"host": "db"})
    definition = {
        "mode": "managed",
        "connector": "postgres",
        "parameters": {"host": "other"},
        "target": {"path": "Elsewhere"},
    }
    assert connection_issues(value, definition) == [
        "connector",
        "parameters.host",
        "reference.location",
    ]
    definition["mode"] = "existing"
    assert "parameters.host" not in connection_issues(value, definition)


def test_verify_report_raises_structured_mismatches_and_reads_only(bi_project):
    registry = load_registry()
    store = BIResourceStore(registry, readonly=True)
    connection = Connection(id="ch", name="CH", type="clickhouse")
    entities = {"dataset:sales": dataset(), "connection:ch": connection}
    with DataLensClientYC(auth=NoAuthProvider()) as client:
        report = verify_resources(client, store, registry, set(entities), entities)
        assert report["verified_resources"] == sorted(entities)
        assert not report["mismatches"]
        registry.resources["dataset:sales"].definition["calculations"]["Amount"]["formula"] = (
            "SUM(2)"
        )
        with pytest.raises(DataLensUtilsError, match="verification failed") as raised:
            verify_resources(client, store, registry, set(entities), entities)
    assert {"resource": "dataset:sales", "path": "Amount formula"} in raised.value.mismatches
    assert not store.path.exists()


def test_persistence_wrong_returned_identity_keeps_successful_write_checkpoint(bi_project):
    store = BIResourceStore(load_registry())
    store.set_location(SimpleNamespace(key="Offline/Variants/", list_entries=list))
    saved = replace(dataset(), saved_id="draft")
    with pytest.raises(DataLensUtilsError, match="identity differs"):
        store.persisted(
            "dataset:sales", saved, lambda **kwargs: replace(saved, id="wrong"), branch="saved"
        )
    assert store.state["resources"]["dataset:sales"]["id"] == "sales"
    assert (
        store.state["resources"]["dataset:sales"]["pending_write"]["metadata"]["saved_id"]
        == "draft"
    )
    assert (
        entry_description(SimpleNamespace(raw={"annotation": {"description": "Authored"}}))
        == "Authored"
    )
