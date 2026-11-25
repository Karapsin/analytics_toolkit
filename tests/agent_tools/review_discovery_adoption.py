"""Explicit discovery migration requires identical immutable scope and provenance."""

from __future__ import annotations

import copy
import json

import pytest

from agent_tools import local_review_files, review_chunks
from tests.agent_tools.review_chunks import (
    candidate as candidate,  # noqa: PLC0414 - shared fixture or compatibility export.
)
from tests.agent_tools.sessions import (
    source as source,  # noqa: PLC0414 - shared fixture or compatibility export.
)


def migration(candidate):
    root, subject = candidate
    original = review_chunks.propose(root, subject, local_review_files)
    current = copy.deepcopy(original)
    current["subject"]["task_digest"] = "b" * 64
    current["author_plan"] = (
        "Retire complete zero-new discovery chunks on this unchanged candidate."
    )
    current[review_chunks.REUSE_DISCOVERY_KEY] = {
        "task_digest": original["subject"]["task_digest"],
        "plan_sha256": review_chunks.plan_digest(original),
        "discovery_only": True,
    }
    return current, original


def test_original_v1_manifest_and_explicit_procedural_marker_are_both_valid(candidate):
    current, original = migration(candidate)
    assert review_chunks.validate_manifest(original["subject"], original) == original
    assert review_chunks.validate_manifest(current["subject"], current) == current
    assert current["author_plan"] != original["author_plan"]
    assert review_chunks.validate_discovery_reuse(current, original) is None
    unmarked = copy.deepcopy(current)
    unmarked.pop(review_chunks.REUSE_DISCOVERY_KEY)
    assert review_chunks.plan_digest(unmarked) != review_chunks.plan_digest(current)


def test_explicit_install_binds_marker_to_new_subject_and_actual_inventory(candidate):
    root, _ = candidate
    current, original = migration(candidate)
    author = {
        key: value
        for key, value in current.items()
        if key not in {"version", "kind", "subject", "inventory"}
    }
    installed = review_chunks.install(root, current["subject"], local_review_files, author)
    assert installed == current
    assert review_chunks.load(root, current["subject"], local_review_files) == current
    assert review_chunks.validate_discovery_reuse(installed, original) is None
    assert installed["subject"]["task_digest"] != original["subject"]["task_digest"]


@pytest.mark.parametrize(
    "mutation",
    [
        "null",
        "false",
        "integer",
        "string",
        "extra",
        "missing",
        "task_hash",
        "plan_hash",
        "same_task",
    ],
)
def test_migration_marker_is_exact_explicit_and_discovery_only(candidate, mutation):
    current, _ = migration(candidate)
    marker = current[review_chunks.REUSE_DISCOVERY_KEY]
    if mutation == "null":
        current[review_chunks.REUSE_DISCOVERY_KEY] = None
    elif mutation == "false":
        marker["discovery_only"] = False
    elif mutation == "integer":
        marker["discovery_only"] = 1
    elif mutation == "string":
        marker["discovery_only"] = "true"
    elif mutation == "extra":
        marker["approval"] = True
    elif mutation == "missing":
        marker.pop("discovery_only")
    elif mutation == "task_hash":
        marker["task_digest"] = "not-a-task-hash"
    elif mutation == "plan_hash":
        marker["plan_sha256"] = "F" * 64
    else:
        marker["task_digest"] = current["subject"]["task_digest"]
    with pytest.raises(ValueError, match="discovery-only migration marker"):
        review_chunks.validate_manifest(current["subject"], current)


@pytest.mark.parametrize("mutation", ["unmarked", "wrong_task", "wrong_plan", "chained"])
def test_adoption_matches_the_actual_original_unbridged_source_plan(candidate, mutation):
    current, original = migration(candidate)
    marker = current[review_chunks.REUSE_DISCOVERY_KEY]
    if mutation == "unmarked":
        current.pop(review_chunks.REUSE_DISCOVERY_KEY)
    elif mutation == "wrong_task":
        marker["task_digest"] = "c" * 64
    elif mutation == "wrong_plan":
        marker["plan_sha256"] = "c" * 64
    else:
        original[review_chunks.REUSE_DISCOVERY_KEY] = {
            "task_digest": "d" * 64,
            "plan_sha256": "e" * 64,
            "discovery_only": True,
        }
        marker["plan_sha256"] = review_chunks.plan_digest(original)
    with pytest.raises(ValueError, match="original unbridged source plan"):
        review_chunks.validate_discovery_reuse(current, original)


@pytest.mark.parametrize("field", ["repository", "head", "base", "tree", "producer_id"])
def test_discovery_bridge_cannot_change_any_immutable_source_binding(candidate, field):
    current, original = migration(candidate)
    current["subject"][field] = "c" * len(current["subject"][field])
    with pytest.raises(ValueError, match="exact same immutable candidate and assignments"):
        review_chunks.validate_discovery_reuse(current, original)


@pytest.mark.parametrize(
    "field", ["inventory", "components", "chunk_id", "chunk_purpose", "limits"]
)
def test_replanned_or_modified_scopes_cannot_adopt_old_discovery(candidate, field):
    current, original = migration(candidate)
    if field == "inventory":
        current["inventory"][0]["diff_sha256"] = "c" * 64
    elif field == "components":
        current["components"][0]["id"] = "renamed-component"
        current["chunks"][0]["component_ids"] = ["renamed-component"]
    elif field == "chunk_id":
        current["chunks"][0]["id"] = "renamed-chunk"
    elif field == "chunk_purpose":
        current["chunks"][0]["purpose"] = "Changed assigned obligations"
    else:
        current["limits"]["max_workers"] = 3
    with pytest.raises(ValueError, match="exact same immutable candidate and assignments"):
        review_chunks.validate_discovery_reuse(current, original)


def test_marker_never_relaxes_assignment_size_or_inventory_coverage(candidate):
    current, original = migration(candidate)
    current["limits"]["max_paths"] = 1
    with pytest.raises(ValueError, match="Oversized review chunk"):
        review_chunks.validate_discovery_reuse(current, original)


@pytest.mark.parametrize("manifest", [None, {}, {"subject": None}])
def test_discovery_adoption_rejects_missing_manifests_and_subjects(candidate, manifest):
    current, original = migration(candidate)
    with pytest.raises(ValueError, match="complete current and source"):
        review_chunks.validate_discovery_reuse(manifest, original)
    with pytest.raises(ValueError, match="complete current and source"):
        review_chunks.validate_discovery_reuse(current, manifest)


def test_installed_digest_tracks_current_plan_without_reading_diff_inventory(
    candidate, monkeypatch
):
    root, subject = candidate
    plan = review_chunks.load(root, subject, local_review_files)
    original = review_chunks.plan_digest(plan)
    changed = copy.deepcopy(plan)
    changed["author_plan"] = "A replacement author assignment invalidates the old proof."
    review_chunks.install(root, subject, local_review_files, changed)

    def forbidden(*args, **kwargs):
        msg = "Installed digest must not rebuild immutable Git inventory"
        raise AssertionError(msg)

    monkeypatch.setattr(review_chunks, "build_inventory", forbidden)
    current = review_chunks.installed_digest(root, subject)
    assert current == review_chunks.plan_digest(changed)
    assert current != original


@pytest.mark.parametrize("mutation", ["missing", "symlink", "malformed", "encoding", "tampered"])
def test_installed_digest_rejects_unsafe_missing_or_invalid_private_plan(
    candidate, tmp_path, mutation
):
    root, subject = candidate
    review_chunks.load(root, subject, local_review_files)
    target = review_chunks._plan_path(root, subject)
    if mutation == "missing":
        target.unlink()
    elif mutation == "symlink":
        outside = tmp_path / "outside-plan.json"
        target.replace(outside)
        target.symlink_to(outside)
    elif mutation == "malformed":
        target.write_text('{"version":')
    elif mutation == "encoding":
        target.write_bytes(b"\xff")
    else:
        manifest = json.loads(target.read_text())
        manifest["chunks"][0]["paths"].pop()
        target.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match=r"installed review plan|inventory paths"):
        review_chunks.installed_digest(root, subject)


@pytest.mark.parametrize("key", review_chunks.SUBJECT_KEYS)
def test_installed_digest_requires_every_current_subject_field(candidate, key):
    root, subject = candidate
    review_chunks.load(root, subject, local_review_files)
    target = review_chunks._plan_path(root, subject)
    manifest = json.loads(target.read_text())
    manifest["subject"][key] = "c" * len(subject[key])
    target.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="candidate binding"):
        review_chunks.installed_digest(root, subject)
