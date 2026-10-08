"""Family-aware typed creation, identity-preserving updates and verification."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from analytics_toolkit.datalens_utils.errors import DataLensUtilsError
from analytics_toolkit.datalens_utils.session import current_session as session
from analytics_toolkit.datalens_utils.validation.charts import check_chart

from . import editor, ql, wizard

ADAPTERS = {"wizard": wizard, "ql": ql, "editor": editor}


def chart_adapter(definition: Any) -> Any:
    family = definition.get("family", "wizard")
    if family not in ADAPTERS:
        message = f"Unsupported chart family {family!r}."
        raise DataLensUtilsError(message)
    return ADAPTERS[family]


def chart_getter(client: Any, definition: Any) -> Any:
    return chart_adapter(definition).getter(client)


def recipe_fingerprint(definition: Any) -> Any:
    recipe = {key: value for key, value in definition.items() if key != "id"}
    files = list(definition.get("scripts", {}).values())
    if definition.get("query_file"):
        files.append(definition["query_file"])
    sources = {
        relative: (session().paths.project_root / relative).read_bytes().hex() for relative in files
    }
    value = json.dumps({"recipe": recipe, "sources": sources}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _validate_change(adapter: Any, chart: Any, context: Any, datasets: Any, definition: Any) -> Any:
    if adapter is wizard:
        adapter.validate_change(chart, context, datasets, definition)
    else:
        adapter.validate_change(chart, definition)


def _remember(resources: Any, key: Any, definition: Any, phase: Any) -> Any:
    entry = resources.state["resources"][key]
    entry.update(
        family=definition.get("family", "wizard"),
        type=definition["type"],
        recipe_fingerprint=recipe_fingerprint(definition),
    )
    resources.phase(key, phase)


def _publish(adapter: Any, chart: Any) -> Any:
    if adapter is ql:
        return chart.update.mode("publish").execute()
    if adapter is editor:
        return chart.publish_revision(rev_id=chart.saved_id)
    return chart.publish_revision(rev_id=chart.saved_id)


def _structural(issues: Any) -> Any:
    return any(
        issue.endswith("fields") or issue.startswith("local field ") or issue == "sorting"
        for issue in issues
    )


def create_charts(*, context: Any, datasets: Any, definitions: Any) -> Any:  # noqa: C901
    client, resources = context.client, context.resources
    folder = resources.folder_for("widget")
    # Validate every local file, field handle and typed operation before the
    # first create/rename. This is intentionally separate from fingerprinting.
    inspected = {}
    for role, definition in definitions.items():
        adapter = chart_adapter(definition)
        adapter.validate_definition(definition)
        adapter.create_builder(context, datasets, definition, folder).to_spec()
        state = resources.state["resources"].get(f"chart:{role}", {})
        if (
            state.get("family", definition.get("family", "wizard"))
            != definition.get("family", "wizard")
            or state.get("type", definition["type"]) != definition["type"]
        ):
            message = (
                f"Chart {role!r} changed family or visualization; its existing ID was retained."
            )
            raise DataLensUtilsError(message)
        if state.get("id"):
            chart = adapter.getter(client)(by_id=state["id"], branch="saved")
            _validate_change(adapter, chart, context, datasets, definition)
            inspected[chart.id] = chart

    charts = {}
    for role, definition in definitions.items():
        key, name = f"chart:{role}", definition["name"]
        adapter = chart_adapter(definition)
        getter = adapter.getter(client)

        def inspected_getter(*, by_id: Any, getter: Any = getter, **options: Any) -> Any:
            # Reuse the fresh safety read once. Post-move/rename/persist fetches
            # still go to the SDK, so this never caches a changed resource.
            if by_id in inspected and not options:
                return inspected.pop(by_id)
            return getter(by_id=by_id, **options)

        chart = resources.existing(key, name, inspected_getter, scope="widget")
        if chart is None:
            chart = resources.create(
                key,
                name,
                adapter.create_builder(context, datasets, definition, folder),
                getter,
                scope="widget",
            )
        _validate_change(adapter, chart, context, datasets, definition)
        chart = resources.rename(key, chart, name, getter)
        issues = check_chart(chart, context=context, datasets=datasets, definition=definition)
        if issues:
            chart = getter(by_id=chart.id, branch="saved")
            _remember(resources, key, definition, "rebuilding")
            layered = adapter is wizard and definition["type"] in {"combined_chart", "geolayer"}
            removable = [field for field in chart.fields if field.guid] if adapter is wizard else []
            if adapter is wizard and _structural(issues) and not layered and removable:
                # Removed GUIDs cannot be restored in the same Wizard update.
                # Publish stays intact while the saved draft is rebuilt.
                update = chart.update
                for field in removable:
                    update.delete_field(field)
                chart = resources.persisted(
                    key, update.mode("save").execute(), getter, branch="saved"
                )
            chart = resources.persisted(
                key,
                adapter.configure(chart.update, context, datasets, definition)
                .mode("publish")
                .execute(),
                getter,
            )
        elif chart.saved_id != chart.published_id:
            chart = getter(by_id=chart.id, branch="saved")
            chart = resources.persisted(key, _publish(adapter, chart), getter)
        issues = check_chart(chart, context=context, datasets=datasets, definition=definition)
        if issues:
            message = f"Chart {name!r} does not match its recipe: {', '.join(issues)}."
            raise DataLensUtilsError(message)
        _remember(resources, key, definition, "published")
        charts[role] = chart
    return charts


def configure_chart(builder: Any, dataset: Any, definition: Any) -> Any:
    """Backward-compatible Wizard adapter for existing recipe callers."""
    return wizard.configure(
        builder,
        None,
        {definition.get("dataset", "sales"): dataset},
        definition,
        creation=not hasattr(builder, "chart"),
    )
