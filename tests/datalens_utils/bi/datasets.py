"""Typed dataset joins, rule ownership and invalidation configuration."""

from dataclasses import replace
from unittest.mock import Mock

import pytest
from analytics_toolkit.datalens_utils.bi_datasets import (
    avatar_for,
    cache_matches,
    cache_source,
    configure_default_filters,
    configure_relations,
    configure_rls,
    configure_sources,
    field_handles,
    source_identities,
)
from analytics_toolkit.datalens_utils.errors import DataLensCapabilityError, DataLensUtilsError
from datalens_sdk import Connection, DataLensClientYC, NoAuthProvider, Source

from tests.datalens_utils._support.bi import dataset


def joined():
    value = dataset()
    right = Source(
        id="right",
        title="right",
        source_type="CH_TABLE",
        connection_id="ch",
        connection_type="clickhouse",
        parameters={"db_name": "example", "table_name": "calendar"},
    )
    return replace(
        value,
        sources=(*value.sources, right),
        source_avatars=(*value.source_avatars, {"id": "right-avatar", "source_id": "right"}),
        result_schema=(
            *value.result_schema,
            {**value.result_schema[0], "guid": "right-category", "avatar_id": "right-avatar"},
        ),
    )


@pytest.mark.parametrize("kind", ["inner", "left", "right", "full"])
def test_join_type_only_update_retains_sources_and_relation(bi_project, kind):
    value = joined()
    relation = {
        "id": "relation",
        "left_avatar_id": "avatar",
        "right_avatar_id": "right-avatar",
        "join_type": "inner",
        "type": "inner",
        "drop_duplicates": False,
        "conditions": [
            {"left": {"source": "category"}, "right": {"source": "category"}, "operator": "eq"}
        ],
    }
    value = replace(value, avatar_relations=(relation,))
    definition = {
        "relations": {
            "calendar": {
                "id": "relation",
                "left": "source",
                "right": "right",
                "type": kind,
                "conditions": [{"left": "category", "right": "category", "operator": "eq"}],
            }
        }
    }
    update = value.update
    owned = configure_relations(
        update, value, definition, {"source": value.sources[0], "right": value.sources[1]}, {}
    )
    assert owned == {"calendar": "relation"}
    actions = update.to_spec().actions
    assert len(actions) == (0 if kind == "inner" else 1)
    assert value.relations[0]["id"] == "relation"


def test_ambiguous_duplicate_column_join_edit_fails_before_write(bi_project):
    value = joined()
    definition = {
        "relations": {
            "calendar": {
                "left": "source",
                "right": "right",
                "type": "left",
                "conditions": [{"left": "category", "right": "category", "operator": "eq"}],
            }
        }
    }
    update = Mock()
    with pytest.raises(DataLensCapabilityError, match="ambiguous"):
        configure_relations(
            update, value, definition, {"source": value.sources[0], "right": value.sources[1]}, {}
        )
    update.execute.assert_not_called()
    update.add_relation.assert_not_called()


def test_source_qualified_fields_resolve_duplicate_columns(bi_project):
    value = joined()
    definition = {
        "sources": {"source": {}, "right": {}},
        "fields": {
            "left": {"source": "source", "column": "category"},
            "right": {"source": "right", "column": "category"},
        },
    }
    handles = field_handles(
        value, definition, {"source": value.sources[0], "right": value.sources[1]}
    )
    assert handles["left"].guid == "category"
    assert handles["right"].guid == "right-category"
    with pytest.raises(DataLensUtilsError, match="avatar"):
        avatar_for(value, value.sources[0], "unknown")
    with pytest.raises(DataLensUtilsError, match="topology"):
        source_identities(value, {"sources": {"missing": {}}}, {})


def rule(subject, *, value="west", pattern="value"):
    return {
        "field_guid": "category",
        "subject": {"subject_id": subject, "subject_type": "user"},
        "allowed_value": value,
        "pattern_type": pattern,
    }


def test_rls_removal_preserves_unmanaged_rules_in_one_builder(bi_project):
    value = replace(dataset(), rls2={"category": (rule("owned"), rule("other"))})
    update = Mock()
    ownership = configure_rls(
        update, value, {"rls": {}}, {}, {"rls_owned": {"category": [["user", "owned"]]}}
    )
    assert ownership == {}
    update.delete_rls.assert_called_once_with(field="category")
    update.add_rls.assert_called_once_with(
        field="category",
        subject_id="other",
        subject_type="user",
        allowed_value="west",
        pattern_type="value",
    )
    update.execute.assert_not_called()


def test_omitted_rls_preserves_unknown_remote_forms(bi_project):
    value = replace(dataset(), rls2={"category": (rule("other", pattern="future"),)})
    update = Mock()
    assert configure_rls(
        update, value, {}, {}, {"rls_owned": {"category": [["user", "owned"]]}}
    ) == {"category": [["user", "owned"]]}
    assert not update.method_calls
    with pytest.raises(DataLensUtilsError, match="unknown remote RLS"):
        configure_rls(
            update, value, {"rls": {}}, {}, {"rls_owned": {"category": [["user", "owned"]]}}
        )


@pytest.mark.parametrize("mode", ["off", "sql", "formula"])
def test_cache_source_typed_modes_never_execute_queries(bi_project, mode):
    root = bi_project.paths.project_root
    path = root / "assets/sql/cache.sql"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("SELECT 1")
    cache = {"mode": mode}
    if mode == "sql":
        cache["sql_file"] = "assets/sql/cache.sql"
    if mode == "formula":
        cache["field"] = {
            "guid": "cache",
            "formula": {"formula": "MAX([Amount])", "guid_formula": "MAX([amount])"},
        }
    cache["filters"] = [
        {"key": "filter", "field": "category", "operation": "IN", "values": ["west"]}
    ]
    value = dataset()
    typed = cache_source(
        {"cache_invalidation": cache}, {"category": value.fields.by_guid("category")}
    )
    payload = (
        value.update.update_cache_invalidation_source(source=typed)
        .to_spec()
        .actions[-1]["cache_invalidation_source"]
    )
    assert payload["mode"] == mode
    assert cache_matches(
        replace(value, raw={"dataset": {"cache_invalidation_source": payload}}), typed
    )
    assert not cache_matches(value, typed)


def test_removed_managed_default_filter_keeps_unrelated_filter(bi_project):
    value = replace(
        dataset(),
        obligatory_filters=(
            {"id": "owned", "field_guid": "category", "default_filters": []},
            {"id": "unmanaged", "field_guid": "amount", "default_filters": []},
        ),
    )
    update = Mock()
    assert (
        configure_default_filters(
            update,
            value,
            {"default_filters": []},
            {},
            {"default_filters_owned": {"category": "owned"}},
        )
        == {}
    )
    update.delete_default_filter.assert_called_once_with(filter_id="owned")


def test_omitted_source_defaults_are_preserved_during_updates(bi_project):
    value = dataset()
    source = replace(
        value.sources[0], parameters={**value.sources[0].parameters, "driver_default": "retained"}
    )
    definition = {
        "sources": {
            "source": {
                "connection": "ch",
                "factory": "ch_table",
                "parameters": dict(value.sources[0].parameters),
            }
        }
    }
    connection = Connection(id="ch", name="CH", type="clickhouse", installation="yacloud")
    update = Mock()
    with DataLensClientYC(auth=NoAuthProvider()) as client:
        configure_sources(update, definition, {"source": source}, {"ch": connection}, client)
        update.update_source.assert_not_called()
        definition["sources"]["source"]["parameters"]["table_name"] = "changed"
        configure_sources(update, definition, {"source": source}, {"ch": connection}, client)
    update.update_source.assert_called_once_with(
        source_id="source-id",
        parameters={"db_name": "example", "table_name": "changed", "driver_default": "retained"},
    )
