"""Content-addressed, non-executable task archives outside candidate Git trees.

The candidate's existing signed task trailers remain the authority. Archive
commits contain only public task data and readable history, never approvals.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import re
from typing import Any

MARKER = "<!-- agent-task-archive-v1\n"
MAX_CONTEXT_BYTES = 8 * 1024 * 1024
CHUNK_BYTES = 96 * 1024
MAX_PAGE_BYTES = 96 * 1024
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_FILES = 2048
IDENTITY = {
    "name": "Analytics Toolkit Task Archive",
    "email": "task-archive@invalid.example",
    "date": "2000-01-01T00:00:00Z",
}
GIT_IDENTITY = "Analytics Toolkit Task Archive <task-archive@invalid.example> 946684800 +0000"
OID = re.compile(r"[0-9a-f]{40}")
DIGEST = re.compile(r"[0-9a-f]{64}")


def oid(kind: str, payload: bytes) -> str:
    # Git's object identifier, not the security digest. Task content uses SHA-256.
    return hashlib.sha1(
        kind.encode() + b" " + str(len(payload)).encode() + b"\0" + payload
    ).hexdigest()


def readable(value: dict[str, Any]) -> dict[str, bytes]:
    """Keep complete user messages, assistant evidence and question/answer links."""
    pages: list[list[str]] = [[]]
    sizes = [0]
    targets: dict[int, str] = {}

    def append(block: str) -> str:
        size = len(block.encode("utf-8"))
        if size > MAX_PAGE_BYTES:
            raise ValueError("Task archive page exceeds its bounded transport limit.")
        if sizes[-1] + size > MAX_PAGE_BYTES - 256:
            pages.append([])
            sizes.append(0)
        pages[-1].append(block)
        sizes[-1] += size
        if len(pages) > MAX_FILES:
            raise ValueError("Task archive has too many history pages.")
        return "history-" + str(len(pages) - 1).zfill(6) + ".md"

    def section(title: str, text: str, anchor: str = "") -> str:
        first = ""
        # Splitting raw Unicode before escaping cannot cut an entity or HTML tag.
        for start in range(0, max(1, len(text)), 12000):
            heading = title + (" (continued)" if start else "")
            label = '<a id="' + anchor + '"></a>\n' if anchor and not start else ""
            path = append(
                label
                + "## "
                + heading
                + "\n\n<pre>"
                + html.escape(text[start : start + 12000])
                + "</pre>\n\n"
            )
            first = first or path
        return first

    messages = [value["starting_prompt"], *value["corrections"]]
    for sequence, text in enumerate(messages):
        anchor = "task-message-" + str(sequence)
        title = "Starting user prompt" if not sequence else "User correction " + str(sequence)
        targets[sequence] = section(title, text, anchor) + "#" + anchor
    for sequence, question in enumerate(value.get("questions", []), 1):
        section("Agent question " + str(sequence), question["text"])
        index = question["answer_index"]
        answer = (
            "Unanswered"
            if index is None
            else "[Complete user correction " + str(index + 1) + "](" + targets[index + 1] + ")"
        )
        append("**User answer:** " + answer + "\n\n")
    for field in ("plans", "images", "replacements", "decisions"):
        if value.get(field):
            section(
                "Assistant evidence: " + field,
                json.dumps(value[field], ensure_ascii=False, indent=2, sort_keys=True),
            )
    files = {}
    for i, blocks in enumerate(pages):
        previous = "[Previous part](history-" + str(i - 1).zfill(6) + ".md) | " if i else ""
        following = (
            " | [Next part](history-" + str(i + 1).zfill(6) + ".md)" if i + 1 < len(pages) else ""
        )
        navigation = previous + "[Task archive index](README.md)" + following + "\n\n"
        files["history-" + str(i).zfill(6) + ".md"] = (navigation + "".join(blocks)).encode("utf-8")
    index = (
        "# Complete task evidence\n\n"
        "User messages appear in chronological order. Agent questions link to their full user answers. "
        "Assistant plans and automated decisions are evidence, not user instructions or approvals. "
        "Plan approval indices and image paths refer to the original task records. "
        "The candidate's signed task digest authenticates this archive; the archive grants no authority.\n\n"
    )
    index += "".join(
        "- [History part " + str(i + 1) + "](" + name + ")\n" for i, name in enumerate(files)
    )
    files["README.md"] = index.encode("utf-8")
    return files


def build(value: dict[str, Any], payload: bytes) -> dict[str, Any]:
    if not 0 < len(payload) <= MAX_CONTEXT_BYTES:
        raise ValueError("Task context exceeds its bounded archive transport limit.")
    digest = hashlib.sha256(payload).hexdigest()
    files = readable(value)
    parts = []
    for start in range(0, len(payload), CHUNK_BYTES):
        chunk = payload[start : start + CHUNK_BYTES]
        name = "context-" + str(len(parts)).zfill(6) + ".jsonpart"
        files[name] = chunk
        parts.append(
            {
                "oid": oid("blob", chunk),
                "sha256": hashlib.sha256(chunk).hexdigest(),
                "bytes": len(chunk),
            }
        )
    if len(files) > MAX_FILES or sum(map(len, files.values())) > MAX_ARCHIVE_BYTES:
        raise ValueError("Task archive exceeds its bounded file or byte budget.")
    entries = [
        {"path": name, "mode": "100644", "type": "blob", "sha": oid("blob", data)}
        for name, data in sorted(files.items())
    ]
    tree = b"".join(
        b"100644 " + item["path"].encode() + b"\0" + bytes.fromhex(item["sha"]) for item in entries
    )
    tree_id = oid("tree", tree)
    message = "Task evidence SHA-256: " + digest + "\n"
    commit = (
        "tree "
        + tree_id
        + "\nauthor "
        + GIT_IDENTITY
        + "\ncommitter "
        + GIT_IDENTITY
        + "\n\n"
        + message
    ).encode()
    manifest = {
        "version": 1,
        "sha256": digest,
        "bytes": len(payload),
        "parts": parts,
        "commit": oid("commit", commit),
    }
    return {
        "files": files,
        "tree": tree_id,
        "entries": entries,
        "message": message,
        "manifest": manifest,
        "ref": "refs/tags/agent-task-evidence/v1/" + digest,
    }


def render(bundle: dict[str, Any], repository: str | None = None) -> str:
    manifest = bundle["manifest"]
    commit = manifest["commit"]
    if repository is not None:
        if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) is None:
            raise ValueError("Task archive requires a repository identity.")
        target = (
            "[Complete task history](https://github.com/"
            + repository
            + "/blob/"
            + commit
            + "/README.md)"
        )
    else:
        # GitHub autolinks a bare commit ID in its repository's PR body.
        target = "Complete task history: " + commit + " (open README.md)"
    encoded = base64.b64encode(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    ).decode()
    return (
        "## User task\n\n" + target + ". Every original message, correction, plan, "
        "agent question, user answer and image record is retained without summarization. "
        "The signed head binds the complete task digest; archive data cannot approve changes.\n\n"
        + MARKER
        + encoded
        + "\n-->\n"
    )


def validate_manifest(value: Any) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or set(value) != {"version", "sha256", "bytes", "parts", "commit"}
        or type(value["version"]) is not int
        or value["version"] != 1
        or not isinstance(value["sha256"], str)
        or DIGEST.fullmatch(value["sha256"]) is None
        or not isinstance(value["commit"], str)
        or OID.fullmatch(value["commit"]) is None
        or type(value["bytes"]) is not int
        or not 0 < value["bytes"] <= MAX_CONTEXT_BYTES
        or not isinstance(value["parts"], list)
        or len(value["parts"]) != (value["bytes"] + CHUNK_BYTES - 1) // CHUNK_BYTES
    ):
        raise ValueError("Invalid task archive manifest.")
    for sequence, part in enumerate(value["parts"]):
        expected = min(CHUNK_BYTES, value["bytes"] - sequence * CHUNK_BYTES)
        if (
            not isinstance(part, dict)
            or set(part) != {"oid", "sha256", "bytes"}
            or not isinstance(part["oid"], str)
            or OID.fullmatch(part["oid"]) is None
            or not isinstance(part["sha256"], str)
            or DIGEST.fullmatch(part["sha256"]) is None
            or type(part["bytes"]) is not int
            or part["bytes"] != expected
        ):
            raise ValueError("Invalid task archive chunk.")
    return value


def read(manifest: dict[str, Any], api: Any) -> bytes:
    validate_manifest(manifest)
    if api is None:
        raise ValueError("Task archive requires a repository-bound blob reader.")
    chunks = []
    for part in manifest["parts"]:
        value = api("git/blobs/" + part["oid"])
        if (
            not isinstance(value, dict)
            or value.get("sha") != part["oid"]
            or value.get("encoding") != "base64"
            or type(value.get("size")) is not int
            or value["size"] != part["bytes"]
            or not isinstance(value.get("content"), str)
            or len(value["content"]) > 2 * CHUNK_BYTES
        ):
            raise ValueError("Invalid task archive blob response.")
        chunk = base64.b64decode(value["content"].replace("\n", ""), validate=True)
        if (
            len(chunk) != part["bytes"]
            or oid("blob", chunk) != part["oid"]
            or hashlib.sha256(chunk).hexdigest() != part["sha256"]
        ):
            raise ValueError("Task archive chunk is missing or has changed.")
        chunks.append(chunk)
    payload = b"".join(chunks)
    if (
        len(payload) != manifest["bytes"]
        or hashlib.sha256(payload).hexdigest() != manifest["sha256"]
    ):
        raise ValueError("Task archive is incomplete or has changed.")
    return payload


def verify_published(bundle: dict[str, Any], api: Any) -> None:
    """Require the complete immutable readable archive, not only detached chunks."""
    ref = api("git/ref/" + bundle["ref"][5:])
    if (
        not isinstance(ref, dict)
        or ref.get("ref") != bundle["ref"]
        or not isinstance(ref.get("object"), dict)
        or ref["object"].get("type") != "commit"
        or ref["object"].get("sha") != bundle["manifest"]["commit"]
    ):
        raise ValueError("Task archive immutable reference is missing or has changed.")
    commit = api("git/commits/" + bundle["manifest"]["commit"])
    if (
        not isinstance(commit, dict)
        or commit.get("sha") != bundle["manifest"]["commit"]
        or commit.get("parents") != []
        or not isinstance(commit.get("tree"), dict)
        or commit["tree"].get("sha") != bundle["tree"]
        or commit.get("message") not in (bundle["message"], bundle["message"][:-1])
    ):
        raise ValueError("Task archive commit is missing or has changed.")
    tree = api("git/trees/" + bundle["tree"])
    if (
        not isinstance(tree, dict)
        or tree.get("sha") != bundle["tree"]
        or tree.get("truncated") is not False
        or not isinstance(tree.get("tree"), list)
        or len(tree["tree"]) != len(bundle["entries"])
        or any(not isinstance(entry, dict) for entry in tree["tree"])
    ):
        raise ValueError("Task archive readable tree is missing or incomplete.")
    entries = [
        {field: entry.get(field) for field in ("path", "mode", "type", "sha")}
        for entry in tree["tree"]
    ]
    if entries != bundle["entries"]:
        raise ValueError("Task archive readable tree differs from its canonical history.")


def publish(bundle: dict[str, Any], api: Any) -> None:
    """Create one immutable archive ref; uncertain creates reconcile by identity."""
    commit = bundle["manifest"]["commit"]

    def existing() -> bool:
        values = api("git/matching-refs/" + bundle["ref"][5:])
        if not isinstance(values, list) or any(not isinstance(item, dict) for item in values):
            raise ValueError("Task archive reference lookup is unavailable.")
        matches = [item for item in values if item.get("ref") == bundle["ref"]]
        if not matches:
            return False
        # GitHub includes object.url as well; compare identity, never overwrite.
        obj = matches[0].get("object", {}) if len(matches) == 1 else {}
        if (
            len(matches) != 1
            or not isinstance(obj, dict)
            or obj.get("type") != "commit"
            or obj.get("sha") != commit
        ):
            raise ValueError("Task archive reference has a conflicting identity.")
        return True

    if existing():
        return
    for entry in bundle["entries"]:
        data = bundle["files"][entry["path"]]
        result = api(
            "git/blobs", "POST", {"content": base64.b64encode(data).decode(), "encoding": "base64"}
        )
        if result.get("sha") != entry["sha"]:
            raise ValueError("Published task blob differs from its preflight identity.")
    if api("git/trees", "POST", {"tree": bundle["entries"]}).get("sha") != bundle["tree"]:
        raise ValueError("Published task tree differs from its preflight identity.")
    result = api(
        "git/commits",
        "POST",
        {
            "tree": bundle["tree"],
            "parents": [],
            "message": bundle["message"],
            "author": IDENTITY,
            "committer": IDENTITY,
        },
    )
    if result.get("sha") != commit:
        raise ValueError("Published task commit differs from its preflight identity.")
    try:
        api("git/refs", "POST", {"ref": bundle["ref"], "sha": commit})
    except (RuntimeError, OSError):
        if not existing():
            raise
    if not existing():
        raise ValueError("Task archive publication was not confirmed.")
