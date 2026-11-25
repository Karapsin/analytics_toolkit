"""Select immutable scoped clearances without rewriting their signed origins."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agent_tools import review_chunks

if TYPE_CHECKING:
    from collections.abc import Callable


def origin(wave: dict[str, Any]) -> dict[str, Any]:
    return {
        "subject": wave["subject"],
        "manifest": wave["manifest"],
        "wave_id": wave["id"],
        "iteration": wave["iteration"],
    }


def compatible(current: dict[str, Any], source: dict[str, Any], *, discovery_only: bool) -> bool:
    if current == source:
        return True
    if not discovery_only or "reuse_discovery_from" not in current:
        return False
    marker = current["reuse_discovery_from"]
    if review_chunks.plan_digest(source) != marker["plan_sha256"]:
        return False
    review_chunks.validate_discovery_reuse(current, source)
    return True


def latest(
    state: dict[str, Any],
    manifest: dict[str, Any],
    *,
    discovery_only: bool,
    complete: Callable[[dict[str, Any], list[dict[str, Any]]], bool],
) -> dict[str, tuple[dict[str, Any], dict[str, Any]]]:
    """Return latest observations; a later failed attempt invalidates an older clearance."""
    selected = {}
    for wave in state["waves"]:
        if wave["discovery_only"] is not discovery_only or not compatible(
            manifest, wave["manifest"], discovery_only=discovery_only
        ):
            continue
        source = origin(wave)
        for event in wave["outcomes"]:
            chunk_id = event["chunk_id"]
            selected.pop(chunk_id, None)
            child = event.get("result")
            if child is None:
                continue
            # Only completed zero-new observations can cross the explicit diagnostic bridge.
            if manifest != wave["manifest"] and (
                not complete(child["payload"], source["iteration"]["recorded_findings"])
                or child["payload"]["verdict"].get("findings")
            ):
                continue
            selected[chunk_id] = (child, source)
    return selected


def verify_origin(
    current: dict[str, Any],
    source: dict[str, Any],
    payload: dict[str, Any],
    *,
    discovery_only: bool,
) -> None:
    if not compatible(current, source["manifest"], discovery_only=discovery_only):
        msg = "Scoped clearance belongs to a different candidate or assignment"
        raise ValueError(msg)
    if source["subject"] != source["manifest"]["subject"]:
        msg = "Scoped clearance origin subject does not match its original plan"
        raise ValueError(msg)
    if payload.get("discovery_only") is not discovery_only:
        msg = "Discovery-only scoped clearances cannot supply final approval"
        raise ValueError(msg)
    if current == source["manifest"] and payload.get("review_procedure") != "discovery-chunks-v4":
        msg = "Current scoped clearance requires the V4 retirement procedure"
        raise ValueError(msg)
    if current != source["manifest"] and (
        payload.get("review_procedure") != "discovery-chunks-v3"
        or payload["verdict"].get("findings")
    ):
        msg = "Only original zero-new V3 discovery receipts may use the diagnostic bridge"
        raise ValueError(msg)
