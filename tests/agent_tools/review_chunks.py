"""Bounded author assignments cannot reduce immutable whole-wave coverage."""

from __future__ import annotations

import copy
import json

import pytest

from agent_tools import local_review, local_review_files, review_chunks
from tests._support.paths import REPO_ROOT
from tests.agent_tools.local_review_evidence import (
    advance_dev,
    capture_review,
    merge_dev,
)
from tests.agent_tools.local_review_evidence import (
    candidate as _session_candidate_fixture,
)
from tests.agent_tools.sessions import git
from tests.agent_tools.sessions import (
    source as source,  # noqa: PLC0414 - shared fixture or compatibility export.
)

session_candidate = _session_candidate_fixture


@pytest.fixture
def candidate(source):
    base = git(source, "rev-parse", "HEAD")
    (source / "seed").unlink()
    for path in ("package/a.py", "tests/a.py", "docs/note.md"):
        target = source / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("value = 1\n")
    git(source, "add", "-A")
    git(source, "commit", "-m", "Candidate including deletion")
    subject = {
        "repository": "owner/repository",
        "head": git(source, "rev-parse", "HEAD"),
        "base": base,
        "tree": git(source, "rev-parse", "HEAD^{tree}"),
        "task_digest": "a" * 64,
        "producer_id": "author:test",
    }
    return source, subject


def proposal(candidate, **kwargs):
    root, subject = candidate
    return review_chunks.propose(root, subject, local_review_files, **kwargs)


def ledger(candidate, chunk, target):
    root, subject = candidate
    files = local_review_files.Files(root, subject["head"], subject["base"], target)
    files.changed_files()
    for path in chunk["paths"]:
        offset = 0
        while True:
            page = files.read_diff(path, offset)
            if page["next_offset"] is None:
                break
            offset = page["next_offset"]
    return target


def test_complete_inventory_includes_deletions_and_small_candidate_is_one_chunk(candidate):
    root, subject = candidate
    plan = proposal(candidate)
    assert plan["subject"] == subject
    assert [item["path"] for item in plan["inventory"]] == [
        "docs/note.md",
        "package/a.py",
        "seed",
        "tests/a.py",
    ]
    assert plan["components"] == [{"id": "component-001", "paths": plan["chunks"][0]["paths"]}]
    assert len(plan["chunks"]) == 1
    assert plan["chunks"][0]["kind"] == "component"
    assert review_chunks.validate(root, subject, plan, local_review_files) == plan


def test_proposal_bounds_primary_and_cross_component_assignments(candidate):
    _, subject = candidate
    plan = proposal(candidate, max_paths=2)
    primary = [chunk for chunk in plan["chunks"] if chunk["kind"] == "component"]
    integrations = [chunk for chunk in plan["chunks"] if chunk["kind"] == "integration"]
    assert len(primary) == 4
    assert len(integrations) == 3
    assert all(len(chunk["paths"]) <= 2 for chunk in plan["chunks"])
    assert {item for chunk in integrations for item in chunk["component_ids"]} == {
        component["id"] for component in plan["components"]
    }
    assert review_chunks.validate_manifest(subject, plan) == plan


@pytest.mark.parametrize(
    "key", ["head", "base", "tree", "task_digest", "producer_id", "repository"]
)
def test_plan_bound_to_every_subject_field(candidate, key):
    root, subject = candidate
    plan = proposal(candidate)
    changed = dict(subject, **{key: "b" * len(subject[key])})
    with pytest.raises(ValueError, match=r"subject|candidate binding"):
        review_chunks.validate(root, changed, plan, local_review_files)


@pytest.mark.parametrize(
    "mutation", ["omit", "duplicate", "unknown", "delete_inventory", "hash", "size", "binary"]
)
def test_author_cannot_forge_inventory_or_primary_coverage(candidate, mutation):
    root, subject = candidate
    plan = proposal(candidate)
    if mutation == "omit":
        plan["chunks"][0]["paths"].pop()
    elif mutation == "duplicate":
        duplicate = copy.deepcopy(plan["chunks"][0])
        duplicate["id"] = "duplicate"
        plan["chunks"].append(duplicate)
    elif mutation == "unknown":
        plan["chunks"][0]["paths"][0] = "outside.py"
    elif mutation == "delete_inventory":
        removed = plan["inventory"].pop()["path"]
        plan["components"][0]["paths"].remove(removed)
        plan["chunks"][0]["paths"].remove(removed)
    elif mutation == "hash":
        plan["inventory"][0]["diff_sha256"] = "0" * 64
    elif mutation == "size":
        plan["inventory"][0]["diff_chars"] = 0
    else:
        plan["inventory"][0]["binary"] = True
    with pytest.raises(ValueError, match=r"inventory|paths|coverage"):
        review_chunks.validate(root, subject, plan, local_review_files)


@pytest.mark.parametrize(
    "mutation", ["orphan", "fake_scope", "oversize", "workers", "duplicate_id"]
)
def test_invalid_cross_component_and_resource_assignments_fail_closed(candidate, mutation):
    _, subject = candidate
    plan = proposal(candidate, max_paths=2)
    if mutation == "orphan":
        plan["chunks"] = [chunk for chunk in plan["chunks"] if chunk["kind"] == "component"]
    elif mutation == "fake_scope":
        integration = next(chunk for chunk in plan["chunks"] if chunk["kind"] == "integration")
        integration["component_ids"] = [plan["components"][0]["id"]]
    elif mutation == "oversize":
        plan["limits"]["max_diff_chars"] = 1
    elif mutation == "workers":
        plan["limits"]["max_workers"] = 9
    else:
        plan["chunks"][1]["id"] = plan["chunks"][0]["id"]
    with pytest.raises(ValueError, match=r"component|scope|Oversized|limits|duplicate"):
        review_chunks.validate_manifest(subject, plan)


def test_install_hydrates_only_derived_fields_and_revalidates_persisted_plan(candidate):
    root, subject = candidate
    plan = proposal(candidate, max_paths=2)
    author = {
        key: value
        for key, value in plan.items()
        if key not in {"version", "kind", "subject", "inventory"}
    }
    assert review_chunks.install(root, subject, local_review_files, author) == plan
    assert review_chunks.load(root, subject, local_review_files) == plan
    target = review_chunks._plan_path(root, subject)
    assert target.stat().st_mode & 0o777 == 0o600
    altered = copy.deepcopy(plan)
    altered["inventory"][0]["diff_sha256"] = "0" * 64
    target.write_text(json.dumps(altered))
    with pytest.raises(ValueError, match="immutable inventory"):
        review_chunks.load(root, subject, local_review_files)


def test_plan_digest_covers_author_purpose_and_assignment(candidate):
    plan = proposal(candidate)
    expected = review_chunks.plan_digest(plan)
    assert review_chunks.plan_digest(json.loads(json.dumps(plan, sort_keys=True))) == expected
    for key in ("author_plan", "chunks"):
        changed = copy.deepcopy(plan)
        if key == "author_plan":
            changed[key] = "A different obligation"
        else:
            changed[key][0]["purpose"] = "A different integration obligation"
        assert review_chunks.plan_digest(changed) != expected


def test_only_whole_bounded_candidate_can_implicitly_install_a_plan(candidate, monkeypatch):
    root, subject = candidate
    plan = review_chunks.load(root, subject, local_review_files)
    assert len(plan["chunks"]) == 1
    assert review_chunks._plan_path(root, subject).is_file()
    review_chunks._plan_path(root, subject).unlink()
    large = proposal(candidate, max_paths=2)
    monkeypatch.setattr(review_chunks, "propose", lambda *args, **kwargs: large)
    with pytest.raises(ValueError, match=r"explicit bounded author review plan.*review-plan"):
        review_chunks.load(root, subject, local_review_files)
    assert not review_chunks._plan_path(root, subject).exists()


def advance_candidate_base(candidate, *, incoming_paths=1, retain_feature_tree=False):
    root, subject = candidate
    git(root, "switch", "-c", "incoming", subject["base"])
    for index in range(incoming_paths):
        path = root / f"incoming-{index}.py"
        path.write_text("INCOMING = 1\n")
        git(root, "add", path.name)
    git(root, "commit", "-m", "Incoming dev changes")
    advanced = git(root, "rev-parse", "HEAD")
    git(root, "switch", "dev")
    options = ["--strategy=ours"] if retain_feature_tree else []
    git(root, "merge", "--no-ff", *options, "incoming", "-m", "Merge incoming dev")
    subject.update(head=git(root, "rev-parse", "HEAD"), tree=git(root, "rev-parse", "HEAD^{tree}"))
    return dict(subject, base=advanced)


def test_implicit_origin_refreshes_only_the_same_head_after_forward_merged_base(candidate):
    root, subject = candidate
    advanced = advance_candidate_base(candidate)
    original = review_chunks.load(root, subject, local_review_files)
    target = review_chunks._plan_path(root, subject)
    origin = review_chunks._origin_path(target)
    assert json.loads(origin.read_text())["plan_sha256"] == review_chunks.plan_digest(original)
    refreshed = review_chunks.load(root, advanced, local_review_files)
    assert refreshed["subject"] == advanced
    assert "incoming-0.py" not in {item["path"] for item in refreshed["inventory"]}
    assert review_chunks.plan_digest(refreshed) != review_chunks.plan_digest(original)
    assert json.loads(origin.read_text())["plan_sha256"] == review_chunks.plan_digest(refreshed)
    assert origin.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("customized", [False, True])
def test_explicit_install_removes_origin_even_for_identical_automatic_plan(candidate, customized):
    root, subject = candidate
    advanced = advance_candidate_base(candidate)
    plan = review_chunks.load(root, subject, local_review_files)
    target = review_chunks._plan_path(root, subject)
    origin = review_chunks._origin_path(target)
    assert origin.is_file()
    if customized:
        plan["author_plan"] = "Explicitly assigned author component obligations"
    review_chunks.install(root, subject, local_review_files, plan)
    assert not origin.exists()
    original = target.read_bytes()
    with pytest.raises(
        ValueError, match=r"explicit author review plan.*stale candidate binding.*review-plan"
    ):
        review_chunks.load(root, advanced, local_review_files)
    assert target.read_bytes() == original
    assert not origin.exists()


@pytest.mark.parametrize(
    "mutation", ["missing", "digest", "subject", "version", "symlink", "malformed"]
)
def test_stale_plan_requires_matching_private_automatic_origin(candidate, tmp_path, mutation):
    root, subject = candidate
    advanced = advance_candidate_base(candidate)
    review_chunks.load(root, subject, local_review_files)
    target = review_chunks._plan_path(root, subject)
    origin = review_chunks._origin_path(target)
    value = json.loads(origin.read_text())
    if mutation == "missing":
        origin.unlink()
    elif mutation == "malformed":
        origin.write_text('{"version":')
    elif mutation == "symlink":
        outside = tmp_path / "outside-origin.json"
        outside.write_text(json.dumps(value))
        origin.unlink()
        origin.symlink_to(outside)
    else:
        if mutation == "digest":
            value["plan_sha256"] = "0" * 64
        elif mutation == "subject":
            value["subject"] = advanced
        else:
            value["version"] = True
        origin.write_text(json.dumps(value))
    original = target.read_bytes()
    with pytest.raises(ValueError, match=r"explicit author review plan.*stale candidate binding"):
        review_chunks.load(root, advanced, local_review_files)
    assert target.read_bytes() == original


def test_base_advance_cannot_automatically_admit_an_oversized_replacement(candidate):
    root, subject = candidate
    advanced = advance_candidate_base(candidate, incoming_paths=31, retain_feature_tree=True)
    original = review_chunks.load(root, subject, local_review_files)
    assert len(original["chunks"]) == 1
    target = review_chunks._plan_path(root, subject)
    before = target.read_bytes()
    origin = review_chunks._origin_path(target).read_bytes()
    with pytest.raises(ValueError, match=r"explicit bounded author review plan.*review-plan"):
        review_chunks.load(root, advanced, local_review_files)
    assert target.read_bytes() == before
    assert review_chunks._origin_path(target).read_bytes() == origin


def test_forged_origin_cannot_reclassify_a_custom_plan_as_automatic(candidate):
    root, subject = candidate
    advanced = advance_candidate_base(candidate)
    plan = review_chunks.load(root, subject, local_review_files)
    target = review_chunks._plan_path(root, subject)
    origin = review_chunks._origin_path(target)
    plan["author_plan"] = "Custom author obligations cannot become automatic"
    target.write_text(json.dumps(plan))
    marker = json.loads(origin.read_text())
    marker["plan_sha256"] = review_chunks.plan_digest(plan)
    origin.write_text(json.dumps(marker))
    original = target.read_bytes()
    with pytest.raises(
        ValueError, match=r"automatic review plan was modified.*explicit author review plan"
    ):
        review_chunks.load(root, advanced, local_review_files)
    assert target.read_bytes() == original


@pytest.mark.parametrize("direction", ["backwards", "unmerged"])
def test_automatic_origin_cannot_follow_a_nonforward_or_unmerged_base(candidate, direction):
    root, subject = candidate
    advanced = advance_candidate_base(candidate)
    if direction == "backwards":
        review_chunks.load(root, advanced, local_review_files)
        requested = subject
    else:
        review_chunks.load(root, subject, local_review_files)
        git(root, "switch", "incoming")
        (root / "unmerged.py").write_text("UNMERGED = 1\n")
        git(root, "add", "unmerged.py")
        git(root, "commit", "-m", "Not merged into candidate")
        requested = dict(subject, base=git(root, "rev-parse", "HEAD"))
        git(root, "switch", "dev")
    target = review_chunks._plan_path(root, subject)
    original = target.read_bytes()
    with pytest.raises(
        ValueError, match=r"cannot follow a non-advancing base.*explicit author review plan"
    ):
        review_chunks.load(root, requested, local_review_files)
    assert target.read_bytes() == original


def test_refreshed_implicit_base_requires_a_new_clean_scope(session_candidate, source, monkeypatch):
    root = session_candidate
    advanced = advance_dev(source)
    head = merge_dev(root)
    subject = local_review.binding(root, head)
    original = review_chunks.load(root, subject, local_review_files)
    calls = capture_review(monkeypatch)
    receipt = local_review.review(root)
    assert len(calls) == 1
    assert all(call["subject"]["base"] == advanced for call in calls)
    assert all(
        call["request"]["review_chunk"]["plan_sha256"] != review_chunks.plan_digest(original)
        for call in calls
    )
    assert len({call["request"]["review_chunk"]["wave_id"] for call in calls}) == 1
    compact = receipt["payload"]["review_iterations"]
    full = json.loads(
        (local_review.directory(root) / ("proof-" + compact["proof_sha256"] + ".json")).read_text()
    )
    rounds = full["waves"]
    assert len(rounds) == 1
    assert len({item["payload"]["reviewer_id"] for item in rounds}) == 1
    assert receipt["payload"]["base"] == advanced


def test_symlinked_private_plan_is_rejected(candidate, tmp_path):
    root, subject = candidate
    target = review_chunks._plan_path(root, subject)
    outside = tmp_path / "outside.json"
    outside.write_text("{}")
    target.symlink_to(outside)
    with pytest.raises(ValueError, match="Unsafe private"):
        review_chunks.load(root, subject, local_review_files)
    with pytest.raises(ValueError, match="Unsafe private"):
        review_chunks.install(root, subject, local_review_files, proposal(candidate))
    assert outside.read_text() == "{}"


def test_scoped_validation_preserves_real_ledger_and_does_not_grant_whole_coverage(
    candidate, tmp_path
):
    root, subject = candidate
    plan = proposal(candidate, max_paths=2)
    chunk = plan["chunks"][0]
    target = ledger(candidate, chunk, tmp_path / "read.jsonl")
    original = target.read_bytes()
    review_chunks.scoped_coverage(local_review_files, root, subject, plan, chunk["id"], target)
    assert target.read_bytes() == original
    with pytest.raises(ValueError, match="complete diff"):
        local_review_files.complete_coverage(
            local_review_files.Files(root, subject["head"], subject["base"], target)
        )


@pytest.mark.parametrize("mutation", ["inventory", "hash", "missing", "geometry"])
def test_scoped_coverage_requires_real_inventory_and_exact_pages(candidate, tmp_path, mutation):
    root, subject = candidate
    plan = proposal(candidate, max_paths=2)
    chunk = plan["chunks"][0]
    target = ledger(candidate, chunk, tmp_path / "read.jsonl")
    events = [json.loads(line) for line in target.read_text().splitlines()]
    if mutation == "inventory":
        events[0]["paths"] = chunk["paths"]
    elif mutation == "hash":
        events[1]["sha256"] = "0" * 64
    elif mutation == "missing":
        events.pop()
    else:
        events[1]["end"] += 100
    target.write_text("\n".join(json.dumps(event) for event in events))
    with pytest.raises(ValueError, match=r"inventory|complete diff|geometry"):
        review_chunks.scoped_coverage(local_review_files, root, subject, plan, chunk["id"], target)


def test_whole_wave_requires_every_independent_integration_ledger(candidate, tmp_path):
    root, subject = candidate
    plan = proposal(candidate, max_paths=2)
    ledgers = {
        chunk["id"]: ledger(candidate, chunk, tmp_path / (chunk["id"] + ".jsonl"))
        for chunk in plan["chunks"]
    }
    originals = {key: value.read_bytes() for key, value in ledgers.items()}
    review_chunks.aggregate_coverage(local_review_files, root, subject, plan, ledgers)
    assert all(target.read_bytes() == originals[key] for key, target in ledgers.items())
    with pytest.raises(ValueError, match="every component and integration"):
        review_chunks.aggregate_coverage(
            local_review_files, root, subject, plan, dict(list(ledgers.items())[:-1])
        )
    repeated = dict(ledgers)
    repeated[plan["chunks"][-1]["id"]] = next(iter(ledgers.values()))
    with pytest.raises(ValueError, match="distinct real"):
        review_chunks.aggregate_coverage(local_review_files, root, subject, plan, repeated)


def test_oversized_single_path_cannot_be_omitted_or_automatically_admitted(candidate):
    with pytest.raises(ValueError, match="bounded page-range support"):
        proposal(candidate, max_diff_chars=1)


def test_scope_keeps_original_binary_image_requirement(candidate, tmp_path):
    root, subject = candidate
    (root / "original.png").write_bytes(b"\x89PNG\r\n\x1a\n\0original")
    git(root, "add", "original.png")
    git(root, "commit", "-m", "Original image")
    subject.update(head=git(root, "rev-parse", "HEAD"), tree=git(root, "rev-parse", "HEAD^{tree}"))
    plan = proposal(candidate)
    chunk = plan["chunks"][0]
    target = ledger(candidate, chunk, tmp_path / "read.jsonl")
    assert next(item for item in plan["inventory"] if item["path"] == "original.png")["binary"]
    with pytest.raises(ValueError, match="Changed binary requires a reviewed PNG"):
        review_chunks.scoped_coverage(local_review_files, root, subject, plan, chunk["id"], target)
    files = local_review_files.Files(root, subject["head"], subject["base"], target)
    files.read_image("original.png")
    review_chunks.scoped_coverage(local_review_files, root, subject, plan, chunk["id"], target)


def test_unicode_diff_scope_still_requires_every_original_page(candidate, tmp_path):
    root, subject = candidate
    (root / "unicode.py").write_text("VALUE = '" + "☃" * 30000 + "'\n")
    git(root, "add", "unicode.py")
    git(root, "commit", "-m", "Unicode pages")
    subject.update(head=git(root, "rev-parse", "HEAD"), tree=git(root, "rev-parse", "HEAD^{tree}"))
    plan = proposal(candidate)
    chunk = plan["chunks"][0]
    target = ledger(candidate, chunk, tmp_path / "read.jsonl")
    review_chunks.scoped_coverage(local_review_files, root, subject, plan, chunk["id"], target)
    events = [json.loads(line) for line in target.read_text().splitlines()]
    pages = [
        event
        for event in events
        if event.get("kind") == "diff" and event.get("path") == "unicode.py"
    ]
    assert len(pages) == 2
    target.write_text("\n".join(json.dumps(event) for event in events if event != pages[1]))
    with pytest.raises(ValueError, match="complete diff"):
        review_chunks.scoped_coverage(local_review_files, root, subject, plan, chunk["id"], target)


def test_trusted_provider_loader_preserves_original_complete_coverage():
    provider = review_chunks.load_provider(REPO_ROOT / "agent_tools/local_review_files.py")
    assert provider.PAGE == local_review_files.PAGE
    assert callable(provider.complete_coverage)
