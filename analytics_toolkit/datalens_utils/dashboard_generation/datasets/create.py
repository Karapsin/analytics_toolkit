"""Create and reconcile named fields over the gallery's existing sources."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from analytics_toolkit.datalens_utils.errors import DataLensUtilsError
from analytics_toolkit.datalens_utils.session import current_session as session


def source_table(context: Any, role: Any, definition: Any) -> Any:
    """Resolve an existing source table from the dataset recipe or context."""
    table = definition.get("table") or getattr(context, "source_tables", {}).get(role)
    if not table or "." not in table:
        message = f"Dataset {role!r} needs an existing database.table source."
        raise DataLensUtilsError(message)
    return table


def source_query(context: Any, definition: Any) -> Any:
    relative = definition.get("projection_file")
    if not relative or Path(relative).is_absolute():
        message = "Dataset projections require a project-relative projection_file."
        raise DataLensUtilsError(message)
    path = (session().paths.project_root / relative).resolve()
    if not path.is_relative_to(session().paths.project_root.resolve()) or not path.is_file():
        message = f"Dataset projection is missing or escapes the project: {relative!r}."
        raise DataLensUtilsError(message)
    query = path.read_text(encoding="utf-8")
    if not query.strip() or re.search(
        r"(?:\bSELECT\s+|,\s*)(?:\w+\.)?\*(?:\s|,)", query, re.IGNORECASE
    ):
        message = "Dataset projections must explicitly name every selected output."
        raise DataLensUtilsError(message)
    tables = getattr(context, "source_tables", {})

    def replace(match: Any) -> Any:
        role = match.group(1).lower()
        table = tables.get(role)
        if not isinstance(table, str) or not re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*", table
        ):
            message = f"Missing or invalid existing source table for projection role {role!r}."
            raise DataLensUtilsError(message)
        return table

    resolved = re.sub(r"__TABLE_([A-Z][A-Z0-9_]*)__", replace, query)
    if re.search(r"__TABLE", resolved, re.IGNORECASE):
        message = "Dataset projection contains an unresolved or malformed source-table token."
        raise DataLensUtilsError(message)
    return resolved


def _calculations(definition: Any) -> Any:
    values = definition.get("calculations", {})
    if isinstance(values, list):
        return {
            value.get("name", value.get("title")): {
                key: item for key, item in value.items() if key not in {"name", "title"}
            }
            for value in values
        }
    return {
        name: {key: item for key, item in value.items() if key not in {"name", "title"}}
        for name, value in values.items()
    }


def validate_definition(context: Any, role: Any, definition: Any) -> Any:
    """Check local source/field contracts before any dataset is persisted."""
    if definition.get("source", "ch_table") == "ch_subselect":
        source_query(context, definition)
    elif definition.get("source", "ch_table") == "ch_table":
        source_table(context, role, definition)
    else:
        message = f"Unsupported dataset source {definition['source']!r}."
        raise DataLensUtilsError(message)
    titles = []
    for physical, expected in definition.get("fields", {}).items():
        title = expected.get("title", physical)
        if not physical or not isinstance(title, str) or not title:
            message = "Dataset source columns and field titles must be nonempty strings."
            raise DataLensUtilsError(message)
        aggregation = expected.get("aggregation", "none")
        if expected.get("kind", "DIMENSION" if aggregation == "none" else "MEASURE") != (
            "DIMENSION" if aggregation == "none" else "MEASURE"
        ):
            message = f"Direct field {physical!r} kind must match its declared aggregation."
            raise DataLensUtilsError(message)
        titles.append(title)
    for name, expected in _calculations(definition).items():
        if (
            not name
            or not isinstance(expected.get("formula"), str)
            or not expected["formula"].strip()
        ):
            message = "Dataset calculations need an explicit title and nonempty formula."
            raise DataLensUtilsError(message)
        if expected.get("aggregation", "none") != "none" and re.search(
            r"\b(?:SUM|AVG|COUNT|COUNTD|MIN|MAX|SUM_IF|AVG_IF|COUNT_IF|RSUM|MAVG|RANK_DENSE|RANK)\s*\(",
            expected["formula"],
            re.IGNORECASE,
        ):
            message = (
                "Calculated field "
                f"{name!r}"
                " already aggregates its raw inputs; set aggregation to 'none'."
            )
            raise DataLensUtilsError(message)
        titles.append(name)
    if len(set(titles)) != len(titles):
        message = f"Dataset {role!r} contains conflicting field titles."
        raise DataLensUtilsError(message)


def _direct_field(dataset: Any, physical: Any) -> Any:
    matches = [
        field
        for field in dataset.fields
        if field.calc_mode == "direct" and field.source == physical
    ]
    if len(matches) != 1:
        message = (
            "Expected one dataset field backed by source column "
            f"{physical!r}"
            "; found "
            f"{len(matches)}"
            "."
        )
        raise DataLensUtilsError(message)
    return matches[0]


def dataset_issues(dataset: Any, definition: Any) -> Any:  # noqa: C901
    """Check actual persisted fields, including formulas and field kinds."""
    issues = []
    for physical, expected in definition.get("fields", {}).items():
        field = _direct_field(dataset, physical)
        if field.title != expected.get("title", physical):
            issues.append(f"{physical} title")
        if expected.get("cast") and field.cast != expected["cast"]:
            issues.append(f"{physical} cast")
        aggregation = expected.get("aggregation", "none")
        if field.aggregation != aggregation or field.type != expected.get(
            "kind", "DIMENSION" if aggregation == "none" else "MEASURE"
        ):
            issues.append(f"{physical} aggregation")
    for name, expected in _calculations(definition).items():
        field = dataset.find_field(name)
        if field is None or field.calc_mode != "formula":
            issues.append(f"{name} calculation")
            continue
        for attribute in ("formula", "cast", "kind", "aggregation"):
            if (
                attribute in expected
                and getattr(field, "type" if attribute == "kind" else attribute)
                != expected[attribute]
            ):
                issues.append(f"{name} {attribute}")  # noqa: PERF401
    for name, aggregation in definition.get("aggregations", {}).items():
        if dataset.fields.by_name(name).aggregation != aggregation:
            issues.append(f"{name} aggregation")
    if dataset.description != definition.get("description", ""):
        issues.append("description")
    return issues


def _configure_fields(update: Any, dataset: Any, definition: Any) -> Any:  # noqa: C901, PLR0912
    for physical, expected in definition.get("fields", {}).items():
        field = _direct_field(dataset, physical)
        aggregation = expected.get("aggregation", "none")
        kind = expected.get("kind", "DIMENSION" if aggregation == "none" else "MEASURE")
        if kind != ("DIMENSION" if aggregation == "none" else "MEASURE"):
            message = f"Direct field {physical!r} kind must match its declared aggregation."
            raise DataLensUtilsError(message)
        if field.aggregation != aggregation or field.type != kind:
            update.change_field_aggregation(field=field, to=aggregation)
        changes = {}
        title = expected.get("title", physical)
        if field.title != title:
            changes["title"] = title
        if expected.get("cast") and field.cast != expected["cast"]:
            changes["cast"] = expected["cast"]
        if changes:
            update.update_field(field=field, **changes)
    for name, expected in _calculations(definition).items():
        field = dataset.find_field(name)
        if field is None:
            update.add_calculation(name=name, **expected)
        elif field.calc_mode != "formula":
            message = f"Calculation {name!r} conflicts with an existing non-formula field."
            raise DataLensUtilsError(message)
        else:
            changes = {
                attribute: value
                for attribute, value in expected.items()
                if getattr(field, "type" if attribute == "kind" else attribute) != value
            }
            if changes:
                update.update_calculation(field=field, **changes)
    for name, aggregation in definition.get("aggregations", {}).items():
        field = dataset.fields.by_name(name)
        if field.aggregation != aggregation:
            update.change_field_aggregation(field=field, to=aggregation)
    if dataset.description != definition.get("description", ""):
        update.description(definition.get("description", ""))
    return update


def create_datasets(*, context: Any, definitions: Any) -> Any:
    client, resources = context.client, context.resources
    folder = resources.folder_for("dataset")
    datasets = {}
    for role, definition in definitions.items():
        validate_definition(context, role, definition)
    for role, definition in definitions.items():
        key, name = f"dataset:{role}", definition["name"]
        source_kind = definition.get("source", "ch_table")
        if source_kind == "ch_subselect":
            query = source_query(context, definition)
        elif source_kind == "ch_table":
            database, table_name = source_table(context, role, definition).split(".", 1)
        else:
            message = f"Unsupported dataset source {source_kind!r}."
            raise DataLensUtilsError(message)
        dataset = resources.existing(key, name, client.get.dataset, scope="dataset")
        if dataset is None:
            factory = client.create.source(using=context.connection)
            source = (
                factory.ch_subselect(alias=role, subsql=query)
                if source_kind == "ch_subselect"
                else factory.ch_table(alias=role, db_name=database, table_name=table_name)
            ).build(strict=True)
            builder = (
                client.create.dataset(name=name, location=folder)
                .sources([source])
                .description(definition.get("description", ""))
            )
            dataset = resources.create(key, name, builder, client.get.dataset, scope="dataset")
        if len(dataset.sources) != 1 or any(
            source.connection_id != context.connection.id
            or (
                source_kind == "ch_subselect"
                and (
                    source.source_type != "CH_SUBSELECT" or source.parameters.get("subsql") != query
                )
            )
            or (
                source_kind == "ch_table"
                and (
                    source.parameters.get("db_name") != database
                    or source.parameters.get("table_name") != table_name
                )
            )
            for source in dataset.sources
        ):
            message = f"Dataset {name!r} uses a different connection or source table."
            raise DataLensUtilsError(message)
        issues = dataset_issues(dataset, definition)
        dataset = resources.rename(key, dataset, name, client.get.dataset)
        if issues:
            dataset = client.get.dataset(by_id=dataset.id, branch="saved")
            update = _configure_fields(dataset.update, dataset, definition)
            dataset = resources.persisted(key, update.mode("publish").execute(), client.get.dataset)
        elif dataset.saved_id != dataset.published_id:
            dataset = resources.persisted(
                key,
                dataset.update.description(definition.get("description", ""))
                .mode("publish")
                .execute(),
                client.get.dataset,
            )
        issues = dataset_issues(dataset, definition)
        if issues:
            message = f"Dataset {name!r} does not match its recipe: {', '.join(issues)}."
            raise DataLensUtilsError(message)
        datasets[role] = dataset
    return datasets
