"""Authored HTML uploads with metadata-only verification and durable revisions."""

from __future__ import annotations

from typing import Any, cast

from .editing.state import fingerprint
from .errors import DataLensConfigurationError, DataLensUtilsError
from .session import current_session
from .settings import asset_path

HTML_MAX_BYTES = 10_485_760


def html_content(definition: dict[str, Any]) -> str:
    try:
        content = asset_path(definition["content_file"]).read_text(encoding="utf-8")
    except (UnicodeDecodeError, KeyError):
        msg = "HTML pages require a project-contained UTF-8 content_file."
        raise DataLensConfigurationError(msg) from None
    if len(content.encode("utf-8")) > HTML_MAX_BYTES:
        msg = "HTML page exceeds the SDK limit of 10,485,760 UTF-8 bytes."
        raise DataLensConfigurationError(msg)
    return cast("str", content)


def reconcile_html(client: Any, store: Any, resource: Any) -> Any:
    definition, key = resource.definition, resource.key
    content = html_content(definition)
    page = store.existing(key, definition["name"], client.get.html_page, scope="html_page")
    old = store.state["resources"].get(key, {})
    source_fingerprint = fingerprint(content)
    written = None
    if page is None:
        builder = client.create.html_page(
            name=definition["name"], location=store.entry_location
        ).content(content)
        if "description" in definition:
            builder.description(definition["description"])
        written = builder.build()
    else:
        store.check_write(key, page)
        if not old.get("source_fingerprint") and not old.get("pending_write"):
            raise DataLensUtilsError(
                key
                + (
                    " has no authored-source baseline. SDK getters cannot retrieve "
                    "source; automatic overwrite refused."
                )
            )
        if old.get("source_fingerprint") != source_fingerprint or (
            "description" in definition
            and getattr(page, "description", page.raw.get("annotation", {}).get("description"))
            != definition["description"]
        ):
            update = page.update.content(content)
            if "description" in definition:
                update.description(definition["description"])
            written = update.mode("publish").execute()
    if written is not None:
        # Record authored source and successful persistence before any verification read.
        store.checkpoint(key, written, scope="html_page", mutation=True)
        old = store.state["resources"][key]
        old.update(
            source_fingerprint=source_fingerprint,
            saved_id=written.saved_id,
            published_id=written.published_id,
        )
        store.save_checkpoint()
        for warning in written.warnings:
            current_session().emit("HTML processing warning:", warning)
        page = client.get.html_page(by_id=written.id, branch="saved")
    if page.saved_id != page.published_id and page.saved_id:
        page = store.persisted(
            key, page.publish_revision(rev_id=page.saved_id), client.get.html_page
        )
        old.update(saved_id=page.saved_id, published_id=page.published_id)
        store.save_checkpoint()
    store.verify_location(page, definition["name"], scope="html_page")
    if (
        not page.saved_id
        or page.saved_id != page.published_id
        or page.published_id != old.get("published_id", page.published_id)
    ):
        raise DataLensUtilsError(key + " HTML saved/published revision relationship differs.")
    return page
