from __future__ import annotations

import base64
import copy
import hashlib
import json
from contextlib import suppress
from html.parser import HTMLParser

import pytest

from tests._support.paths import REPO_ROOT
from tests.repository._support.archive_api import ArchiveAPI, large_context, protocol


class PreformattedText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.inside = False
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag == "pre":
            self.inside = True

    def handle_endtag(self, tag):
        if tag == "pre":
            self.inside = False

    def handle_data(self, data):
        if self.inside:
            self.parts.append(data)


def replace_manifest(proto, body, mutate):
    start, encoded = body.split(proto.ARCHIVE_MARKER)
    encoded, end = encoded.split("\n-->", 1)
    value = json.loads(base64.b64decode(encoded))
    mutate(value)
    return (
        start
        + proto.ARCHIVE_MARKER
        + base64.b64encode(json.dumps(value).encode()).decode()
        + "\n-->"
        + end
    )


@pytest.mark.parametrize("version", [1, 2])
def test_full_history_is_readable_and_bound_to_existing_signed_task_bytes(tmp_path, version):
    proto = protocol()
    value = large_context(proto, version=version)
    original = proto.canonical(value)
    signed = "Feature" + proto.trailers(value)
    planned = proto.publication(value, repository="owner/repo", prefix="Feature description")
    assert len(planned["body"]) < proto.MAX_BODY
    assert proto.ARCHIVE_MARKER in planned["body"]
    assert planned["body"].startswith("Feature description\n\n## User task")
    parser = PreformattedText()
    for name, payload in sorted(planned["archive"]["files"].items()):
        if name.startswith("history-"):
            parser.feed(payload.decode())
    readable = "".join(parser.parts)
    for prompt in [value["starting_prompt"], *value["corrections"]]:
        assert prompt in readable
    for question in value.get("questions", []):
        assert question["text"] in readable
    api = ArchiveAPI(tmp_path)
    body = proto.publish(value, api=api, repository="owner/repo", prefix="Feature description")
    assert body == planned["body"]
    restored = proto.parse(body, api=api)
    assert proto.canonical(restored) == original
    assert proto.verify_commit(restored, signed) == value["producer_id"]
    archive = planned["archive"]
    for entry in archive["entries"]:
        assert (
            api.git("show", archive["manifest"]["commit"] + ":" + entry["path"])
            == archive["files"][entry["path"]]
        )
    assert api.git(
        "rev-list", "--parents", "-n", "1", archive["manifest"]["commit"]
    ).decode().split() == [archive["manifest"]["commit"]]


def test_incompressible_multi_chunk_unicode_context_is_never_shortened(tmp_path):
    proto = protocol()
    value = large_context(proto)
    value["corrections"].append(
        "\u03b1\U0001f4dd"
        + "".join(hashlib.sha256(str(i).encode()).hexdigest() for i in range(9000))
    )
    api = ArchiveAPI(tmp_path)
    planned = proto.publication(value)
    assert len(planned["archive"]["manifest"]["parts"]) > 5
    body = proto.publish(value, api=api, repository="owner/repo")
    assert proto.parse(body, api=api) == value
    assert proto.verify_commit(proto.parse(body, api=api), "Feature" + proto.trailers(value))


def test_every_interrupted_archive_write_is_idempotently_recoverable(tmp_path):
    proto = protocol()
    value = large_context(proto)
    planned = proto.publication(value)
    count = len(planned["archive"]["entries"]) + 3
    for position in range(1, count + 1):
        destination = tmp_path / str(position)
        destination.mkdir()
        api = ArchiveAPI(destination)
        api.fail_after = position
        with suppress(RuntimeError):
            proto.publish(value, api=api, repository="owner/repo")
        api.fail_after = None
        body = proto.publish(value, api=api, repository="owner/repo")
        assert len(api.refs) == 1
        assert proto.parse(body, api=api) == value
        writes = api.mutations
        assert proto.publish(value, api=api, repository="owner/repo") == body
        assert api.mutations == writes


def test_existing_conflicting_archive_ref_is_never_overwritten(tmp_path):
    proto = protocol()
    value = large_context(proto)
    api = ArchiveAPI(tmp_path)
    api.refs[proto.publication(value)["archive"]["ref"]] = "a" * 40
    with pytest.raises(ValueError, match="conflicting identity"):
        proto.publish(value, api=api, repository="owner/repo")
    assert api.mutations == 0


@pytest.mark.parametrize("mutation", ["path", "bytes", "count", "duplicate", "version", "commit"])
def test_malformed_archive_manifests_fail_closed(tmp_path, mutation):
    proto = protocol()
    value = large_context(proto)
    api = ArchiveAPI(tmp_path)
    body = proto.publish(value, api=api, repository="owner/repo")

    def mutate(manifest):
        if mutation == "path":
            manifest["parts"][0]["oid"] = "../../arbitrary-repository"
        elif mutation == "bytes":
            manifest["bytes"] = 10 * 1024 * 1024
        elif mutation == "count":
            manifest["parts"].pop()
        elif mutation == "duplicate":
            manifest["parts"].append(copy.deepcopy(manifest["parts"][0]))
        elif mutation == "version":
            manifest["version"] = True
        else:
            manifest["commit"] = "b" * 40

    api.calls.clear()
    with pytest.raises(ValueError, match="Invalid task archive"):
        proto.parse(replace_manifest(proto, body, mutate), api=api)
    if mutation != "commit":
        assert api.calls == []


def test_missing_tampered_stale_and_mixed_attachments_cannot_inherit_authority(tmp_path):
    proto = protocol()
    value = large_context(proto)
    api = ArchiveAPI(tmp_path)
    body = proto.publish(value, api=api, repository="owner/repo")
    planned = proto.publication(value)
    part = planned["archive"]["manifest"]["parts"][0]
    api.corrupt[part["oid"]] = b"x" * part["bytes"]
    with pytest.raises(ValueError, match="Invalid task archive"):
        proto.parse(body, api=api)
    api.corrupt.clear()
    with pytest.raises(ValueError, match="Invalid task archive"):
        proto.parse(body)
    with pytest.raises(ValueError, match="exactly one"):
        proto.parse(body + proto.render({**proto.legacy(value), "corrections": []}), api=api)
    with pytest.raises(ValueError, match="exactly one"):
        proto.parse(body + body, api=api)
    restored = proto.parse(body, api=api)
    changed = copy.deepcopy(value)
    changed["corrections"].append("Different actual user correction")
    with pytest.raises(ValueError, match="signed head"):
        proto.verify_commit(restored, "Changed head" + proto.trailers(changed))


def test_preflight_bounds_and_redaction_precede_all_archive_mutations(tmp_path):
    proto = protocol()
    value = large_context(proto)
    api = ArchiveAPI(tmp_path)
    with pytest.raises(ValueError, match="description exceeds"):
        proto.publish(value, api=api, repository="owner/repo", prefix="x" * 65000)
    with pytest.raises(ValueError, match="ambiguous"):
        proto.publish(value, api=api, repository="owner/repo", prefix=proto.MARKER)
    assert api.calls == []
    value["corrections"].append("Password=not-for-publication")
    body = proto.publish(value, api=api, repository="owner/repo")
    restored = proto.parse(body, api=api)
    assert "not-for-publication" not in proto.canonical(restored).decode()
    assert "not-for-publication" in value["corrections"][-1]
    assert all(
        b"not-for-publication" not in chunk
        for chunk in proto.publication(value)["archive"]["files"].values()
    )


def test_published_v1_archive_layout_and_signed_context_remain_readable(tmp_path):
    """A format change needs a new codec, never a rewrite of historical v1 data."""
    proto = protocol()
    fixture = json.loads(
        (REPO_ROOT / "tests/repository/_support/fixtures/task_archive_v1.json").read_text()
    )
    api = ArchiveAPI(tmp_path)
    archive = proto.archive_protocol()
    bundle = archive.build(fixture["context"], proto.canonical(fixture["context"]))
    assert bundle["manifest"] == fixture["manifest"]
    assert bundle["files"] == {name: text.encode() for name, text in fixture["files"].items()}
    archive.publish(bundle, api)
    body = (
        proto.ARCHIVE_MARKER
        + base64.b64encode(json.dumps(fixture["manifest"]).encode()).decode()
        + "\n-->\n"
    )
    restored = proto.parse(body, api=api)
    assert restored == fixture["context"]
    assert proto.verify_commit(restored, "Historical head" + proto.trailers(fixture["context"]))


@pytest.mark.parametrize("missing", ["ref", "commit", "tree", "entry", "truncated"])
def test_archive_requires_immutable_ref_commit_and_complete_readable_tree(tmp_path, missing):
    proto = protocol()
    value = large_context(proto)
    api = ArchiveAPI(tmp_path)
    body = proto.publish(value, api=api, repository="owner/repo")

    def incomplete(path, method="GET", data=None):
        result = api(path, method, data)
        if path.startswith("git/ref/") and missing == "ref":
            result["object"]["sha"] = None
        if path.startswith("git/commits/") and missing == "commit":
            result["tree"]["sha"] = "0" * 40
        if path.startswith("git/trees/"):
            if missing == "tree":
                return None
            if missing == "entry":
                result["tree"].pop()
            if missing == "truncated":
                result["truncated"] = True
        return result

    with pytest.raises(ValueError, match="Invalid task archive attachment"):
        proto.parse(body, api=incomplete)


def test_archive_keeps_exact_approved_image_records_and_unanswered_questions(tmp_path):
    proto = protocol()
    value = large_context(proto)
    sha = hashlib.sha256(b"approved-image").hexdigest()
    value["images"] = [
        {
            "reference": "[Image #1]",
            "description": "Original approved image",
            "path": "agent_tools/task_evidence/large-task/" + sha + ".png",
            "sha256": sha,
            "approval_index": 1,
        }
    ]
    value["questions"][0]["answer_index"] = None
    api = ArchiveAPI(tmp_path)
    body = proto.publish(value, api=api, repository="owner/repo")
    assert proto.parse(body, api=api) == value
