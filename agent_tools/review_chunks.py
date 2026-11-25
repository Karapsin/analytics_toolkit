"""Author-owned bounded assignments over one immutable source-review candidate."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import subprocess
import tempfile
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, cast

from agent_tools import sessions

if TYPE_CHECKING:
    from collections.abc import Mapping

SUBJECT_KEYS = ("repository", "head", "base", "tree", "task_digest", "producer_id")
MAX_WORKERS = 8
MAX_PATHS = 30
MAX_DIFF_CHARS = 96000
MIN_INTEGRATION_COMPONENTS = 2
COMPONENT_DIRECTORY_DEPTH = 2
PLAN_KEYS = {
    "version",
    "kind",
    "subject",
    "author_plan",
    "limits",
    "inventory",
    "components",
    "chunks",
}
REUSE_DISCOVERY_KEY = "reuse_discovery_from"


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def plan_digest(manifest: dict[str, Any]) -> str:
    """Bind the complete normalized plan, including author obligations and assignments."""
    return hashlib.sha256(_canonical(manifest)).hexdigest()


def load_provider(source: Path) -> Any:
    """Load a provider already extracted by local_review.trusted_runtime from dev."""
    spec = importlib.util.spec_from_file_location("atk_chunk_review_source", source)
    if spec is None or spec.loader is None:
        msg = "Trusted immutable review provider cannot be loaded"
        raise RuntimeError(msg)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _subject(subject: dict[str, str]) -> None:
    if (
        not isinstance(subject, dict)
        or set(subject) != set(SUBJECT_KEYS)
        or not all(isinstance(value, str) and value for value in subject.values())
        or not all(
            re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", subject[key])
            for key in ("head", "base", "tree")
        )
        or not re.fullmatch(r"[0-9a-f]{64}", subject["task_digest"])
    ):
        msg = "Invalid exact source-review subject"
        raise ValueError(msg)


def _files(provider_module: Any, root: Path, subject: dict[str, str], ledger: Path) -> Any:
    _subject(subject)
    files = provider_module.Files(root, subject["head"], subject["base"], ledger)
    for key in ("head", "base"):
        if files.git("rev-parse", subject[key] + "^{commit}").decode().strip() != subject[key]:
            msg = "Review subject must identify exact immutable commits"
            raise ValueError(msg)
    if files.git("rev-parse", subject["head"] + "^{tree}").decode().strip() != subject["tree"]:
        msg = "Review subject tree does not match the immutable candidate"
        raise ValueError(msg)
    return files


def _diff(files: Any, path: str) -> str:
    return str(
        files.git(
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--no-renames",
            files.base,
            files.head,
            "--",
            ":(literal)" + path,
        ).decode()
    )


def build_inventory(
    root: Path, subject: dict[str, str], provider_module: Any
) -> list[dict[str, Any]]:
    """Derive every path, deletion, diff hash and workload size from trusted Git reads."""
    files = _files(provider_module, root, subject, root / ".git" / "unused-inventory-ledger")
    inventory = []
    for path in files.changed:
        _path(path)
        text = _diff(files, path)
        stats = files.git(
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--numstat",
            files.base,
            files.head,
            "--",
            ":(literal)" + path,
        )
        inventory.append(
            {
                "path": path,
                "diff_sha256": hashlib.sha256(text.encode()).hexdigest(),
                "diff_chars": len(text),
                "binary": stats.startswith(b"-\t-\t"),
            }
        )
    return inventory


def _path(value: Any) -> None:
    if (
        not isinstance(value, str)
        or not value
        or "\0" in value
        or PurePosixPath(value).is_absolute()
        or any(part in {"..", ".connections"} for part in PurePosixPath(value).parts)
        or str(PurePosixPath(value)) != value
    ):
        msg = "Unsafe or noncanonical immutable review path"
        raise ValueError(msg)


def _identifier(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_-]{0,79}", value) is not None


def _text(value: Any, maximum: int) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= maximum


def _limits(value: Any) -> None:
    bounds = {"max_workers": MAX_WORKERS, "max_paths": MAX_PATHS, "max_diff_chars": MAX_DIFF_CHARS}
    if (
        not isinstance(value, dict)
        or set(value) != set(bounds)
        or any(
            type(value[key]) is not int or not 1 <= value[key] <= maximum
            for key, maximum in bounds.items()
        )
    ):
        msg = "Review limits must fit 1..8 workers, 1..30 paths and 1..96000 diff characters"
        raise ValueError(msg)


def _paths(value: Any, known: set[str]) -> list[str]:
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(path, str) and path in known for path in value)
        or len(set(value)) != len(value)
    ):
        msg = "Review assignment contains missing, duplicate or unknown paths"
        raise ValueError(msg)
    return value


def _discovery_reuse_marker(subject: dict[str, str], marker: Any) -> None:
    if (
        not isinstance(marker, dict)
        or set(marker) != {"task_digest", "plan_sha256", "discovery_only"}
        or marker.get("discovery_only") is not True
        or not all(
            isinstance(marker.get(key), str) and re.fullmatch(r"[0-9a-f]{64}", marker[key])
            for key in ("task_digest", "plan_sha256")
        )
        or marker["task_digest"] == subject["task_digest"]
    ):
        msg = "Invalid explicit discovery-only migration marker"
        raise ValueError(msg)


def _validate_plan_header(subject: dict[str, str], manifest: dict[str, Any]) -> None:
    _subject(subject)
    if (
        not isinstance(manifest, dict)
        or not set(manifest) >= PLAN_KEYS
        or set(manifest) - PLAN_KEYS - {REUSE_DISCOVERY_KEY}
        or type(manifest.get("version")) is not int
        or manifest["version"] != 1
        or manifest.get("kind") != "local-review-chunk-plan"
        or manifest.get("subject") != subject
        or not _text(manifest.get("author_plan"), 20000)
    ):
        msg = "Invalid author review plan or exact candidate binding"
        raise ValueError(msg)
    if REUSE_DISCOVERY_KEY in manifest:
        _discovery_reuse_marker(subject, manifest[REUSE_DISCOVERY_KEY])
    _limits(manifest["limits"])


def _inventory_sizes(inventory: Any) -> dict[str, int]:
    if not isinstance(inventory, list) or not inventory:
        msg = "Review plan requires the complete nonempty immutable inventory"
        raise ValueError(msg)
    sizes = {}
    for item in inventory:
        if (
            not isinstance(item, dict)
            or set(item) != {"path", "diff_sha256", "diff_chars", "binary"}
            or not isinstance(item["diff_sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", item["diff_sha256"])
            or type(item["diff_chars"]) is not int
            or item["diff_chars"] < 0
            or type(item["binary"]) is not bool
        ):
            msg = "Invalid immutable review inventory entry"
            raise ValueError(msg)
        _path(item["path"])
        if item["path"] in sizes:
            msg = "Duplicate immutable review inventory path"
            raise ValueError(msg)
        sizes[item["path"]] = item["diff_chars"]
    return sizes


def _component_owners(components: Any, sizes: dict[str, int]) -> dict[str, str]:
    if not isinstance(components, list) or not components:
        msg = "Review plan requires explicit components"
        raise ValueError(msg)
    owners = {}
    component_ids = set()
    for component in components:
        if (
            not isinstance(component, dict)
            or set(component) != {"id", "paths"}
            or not _identifier(component["id"])
            or component["id"] in component_ids
        ):
            msg = "Invalid or duplicate review component"
            raise ValueError(msg)
        component_ids.add(component["id"])
        for path in _paths(component["paths"], set(sizes)):
            if path in owners:
                msg = "Duplicate primary component path ownership"
                raise ValueError(msg)
            owners[path] = component["id"]
    if set(owners) != set(sizes):
        msg = "Components omit immutable review inventory paths"
        raise ValueError(msg)
    return owners


def _chunk_scope(
    chunk: Any,
    ids: set[str],
    owners: dict[str, str],
    sizes: dict[str, int],
    limits: dict[str, int],
) -> tuple[list[str], list[str]]:
    if (
        not isinstance(chunk, dict)
        or set(chunk) != {"id", "kind", "component_ids", "paths", "purpose"}
        or not _identifier(chunk["id"])
        or chunk["id"] in ids
        or chunk["kind"] not in {"component", "integration"}
        or not _text(chunk["purpose"], 4000)
    ):
        msg = "Invalid or duplicate bounded review chunk"
        raise ValueError(msg)
    ids.add(chunk["id"])
    paths = _paths(chunk["paths"], set(sizes))
    scope = chunk["component_ids"]
    if (
        not isinstance(scope, list)
        or not scope
        or not all(isinstance(item, str) and item in set(owners.values()) for item in scope)
        or len(set(scope)) != len(scope)
        or {owners[path] for path in paths} != set(scope)
    ):
        msg = "Chunk scope does not match its explicit components"
        raise ValueError(msg)
    if (
        len(paths) > limits["max_paths"]
        or sum(sizes[path] for path in paths) > limits["max_diff_chars"]
    ):
        msg = "Oversized review chunk; repartition whole paths without reducing coverage"
        raise ValueError(msg)
    return paths, scope


def _validate_assignments(
    chunks: Any, owners: dict[str, str], sizes: dict[str, int], limits: dict[str, int]
) -> None:
    if not isinstance(chunks, list) or not chunks:
        msg = "Review plan requires bounded component and integration chunks"
        raise ValueError(msg)
    ids: set[str] = set()
    primary: Counter[str] = Counter()
    integrated: set[str] = set()
    for chunk in chunks:
        paths, scope = _chunk_scope(chunk, ids, owners, sizes, limits)
        if chunk["kind"] == "component":
            if len(scope) != 1:
                msg = "A primary chunk must have one component owner"
                raise ValueError(msg)
            primary.update(paths)
        else:
            if len(scope) < MIN_INTEGRATION_COMPONENTS:
                msg = "Integration assignments must connect at least two components"
                raise ValueError(msg)
            integrated.update(scope)
    if set(primary) != set(sizes) or any(count != 1 for count in primary.values()):
        msg = "Primary review chunks omit or duplicate immutable inventory paths"
        raise ValueError(msg)
    component_ids = set(owners.values())
    if len(component_ids) > 1 and integrated != component_ids:
        msg = "Review plan leaves a component without cross-component integration coverage"
        raise ValueError(msg)


def validate_manifest(subject: dict[str, str], manifest: dict[str, Any]) -> dict[str, Any]:
    """Validate shape and exact primary coverage; immutable identity is checked by validate."""
    _validate_plan_header(subject, manifest)
    sizes = _inventory_sizes(manifest["inventory"])
    owners = _component_owners(manifest["components"], sizes)
    _validate_assignments(manifest["chunks"], owners, sizes, manifest["limits"])
    return cast("dict[str, Any]", json.loads(_canonical(manifest)))


def _is_mapping(value: Any) -> bool:
    """Distinguish a complete serialized record from a missing migration input."""
    return isinstance(value, dict)


def validate_discovery_reuse(
    current_manifest: dict[str, Any], source_manifest: dict[str, Any]
) -> None:
    """Verify an explicit procedural bridge to an authenticated old discovery plan.

    The engine must authenticate the actual source manifest and receipts, validate
    the current immutable inventory, and limit adoption to diagnostic discovery.
    This comparison never changes receipt task bindings or authorizes approval.
    """
    if not all(_is_mapping(value) for value in (current_manifest, source_manifest)):
        msg = "Discovery migration requires complete current and source manifests"
        raise ValueError(msg)
    current_subject = current_manifest.get("subject")
    source_subject = source_manifest.get("subject")
    if not all(_is_mapping(value) for value in (current_subject, source_subject)):
        msg = "Discovery migration requires complete current and source subjects"
        raise ValueError(msg)
    current = validate_manifest(cast("dict[str, str]", current_subject), current_manifest)
    source = validate_manifest(cast("dict[str, str]", source_subject), source_manifest)
    marker = current.get(REUSE_DISCOVERY_KEY)
    if (
        marker is None
        or REUSE_DISCOVERY_KEY in source
        or marker["task_digest"] != source["subject"]["task_digest"]
        or marker["plan_sha256"] != plan_digest(source)
    ):
        msg = "Discovery migration does not match the original unbridged source plan"
        raise ValueError(msg)
    if any(
        current["subject"][key] != source["subject"][key]
        for key in SUBJECT_KEYS
        if key != "task_digest"
    ) or any(
        current[key] != source[key] for key in ("inventory", "components", "chunks", "limits")
    ):
        msg = "Discovery migration requires the exact same immutable candidate and assignments"
        raise ValueError(msg)


def validate(
    root: Path, subject: dict[str, str], manifest: dict[str, Any], provider_module: Any
) -> dict[str, Any]:
    value = validate_manifest(subject, manifest)
    if value["inventory"] != build_inventory(root, subject, provider_module):
        msg = "Author plan does not match the complete immutable inventory, including deletions"
        raise ValueError(msg)
    return value


def propose(  # noqa: PLR0913 - preserve explicit public plan limits and their defaults.
    root: Path,
    subject: dict[str, str],
    provider_module: Any,
    *,
    max_workers: int = 4,
    max_paths: int = MAX_PATHS,
    max_diff_chars: int = MAX_DIFF_CHARS,
) -> dict[str, Any]:
    """Suggest explicit editable assignments; proposing never installs or approves a plan."""
    limits = {"max_workers": max_workers, "max_paths": max_paths, "max_diff_chars": max_diff_chars}
    _limits(limits)
    inventory = build_inventory(root, subject, provider_module)
    if not inventory:
        msg = "No immutable source changes to assign"
        raise ValueError(msg)
    if any(item["diff_chars"] > max_diff_chars for item in inventory):
        msg = (
            "One immutable diff exceeds the review bound; bounded page-range support is required, "
            "never omit the path"
        )
        raise ValueError(msg)
    small = (
        len(inventory) <= max_paths
        and sum(item["diff_chars"] for item in inventory) <= max_diff_chars
    )
    groups = _proposed_groups(inventory, small=small)
    components, chunks = [], []
    sizes = {item["path"]: item["diff_chars"] for item in inventory}
    for index, paths in enumerate(groups.values(), 1):
        component_id = f"component-{index:03d}"
        components.append({"id": component_id, "paths": paths})
        pending: list[str] = []
        for path in paths:
            if pending and (
                len(pending) == max_paths
                or sum(sizes[item] for item in pending) + sizes[path] > max_diff_chars
            ):
                chunks.append(_chunk(len(chunks), "component", [component_id], pending))
                pending = []
            pending.append(path)
        if pending:
            chunks.append(_chunk(len(chunks), "component", [component_id], pending))
    if len(components) > 1:
        anchor = min(inventory, key=lambda item: item["diff_chars"])["path"]
        anchor_component = next(
            component for component in components if anchor in component["paths"]
        )
        for component in components:
            if component is anchor_component:
                continue
            other = min(component["paths"], key=sizes.__getitem__)
            chunks.append(
                _chunk(
                    len(chunks),
                    "integration",
                    [anchor_component["id"], component["id"]],
                    [anchor, other],
                )
            )
    return validate_manifest(
        subject,
        {
            "version": 1,
            "kind": "local-review-chunk-plan",
            "subject": subject,
            "author_plan": (
                "Review every assigned immutable diff and the current task obligations; inspect "
                "interacting context as needed. Confirm and refine the component boundaries and "
                "integration purposes before installation."
            ),
            "limits": limits,
            "inventory": inventory,
            "components": components,
            "chunks": chunks,
        },
    )


def _proposed_groups(inventory: list[dict[str, Any]], *, small: bool) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    for item in inventory:
        parts = PurePosixPath(item["path"]).parts
        label = (
            "candidate"
            if small
            else "/".join(parts[:COMPONENT_DIRECTORY_DEPTH])
            if len(parts) > COMPONENT_DIRECTORY_DEPTH
            else parts[0]
            if len(parts) > 1
            else "root"
        )
        groups.setdefault(label, []).append(item["path"])
    return groups


def _chunk(index: int, kind: str, component_ids: list[str], paths: list[str]) -> dict[str, Any]:
    return {
        "id": f"chunk-{index + 1:04d}",
        "kind": kind,
        "component_ids": component_ids,
        "paths": paths,
        "purpose": "Assess the assigned component and current task obligations."
        if kind == "component"
        else (
            "Assess shared contracts and interactions between these explicitly assigned "
            "components; read additional immutable context as needed."
        ),
    }


def _plan_path(root: Path, subject: dict[str, str]) -> Path:
    _subject(subject)
    directory = sessions.state_path(root).with_name("atk-local-reviews")
    if directory.is_symlink():
        msg = "Unsafe private review-plan directory"
        raise ValueError(msg)
    directory.mkdir(mode=0o700, exist_ok=True)
    return directory / ("chunks-" + subject["head"] + "-" + subject["task_digest"] + ".json")


def install(
    root: Path, subject: dict[str, str], provider_module: Any, manifest: dict[str, Any]
) -> dict[str, Any]:
    """Persist an explicit author plan, hydrating only omitted immutable derived fields."""
    value = dict(manifest)
    value.setdefault("version", 1)
    value.setdefault("kind", "local-review-chunk-plan")
    value.setdefault("subject", subject)
    if "inventory" not in value:
        value["inventory"] = build_inventory(root, subject, provider_module)
    value = validate(root, subject, value, provider_module)
    _persist_plan(root, subject, value, implicit=False)
    return value


def _origin_path(target: Path) -> Path:
    return target.with_name(target.stem + ".origin.json")


def _write_private_json(target: Path, value: dict[str, Any]) -> None:
    if target.is_symlink():
        msg = "Unsafe private review-plan file"
        raise ValueError(msg)
    with tempfile.NamedTemporaryFile(dir=target.parent, prefix="chunks-", delete=False) as output:
        temporary = Path(output.name)
        output.write(_canonical(value))
    try:
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def _persist_plan(
    root: Path, subject: dict[str, str], value: dict[str, Any], *, implicit: bool
) -> None:
    target = _plan_path(root, subject)
    origin = _origin_path(target)
    if target.is_symlink() or origin.is_symlink():
        msg = "Unsafe private review-plan file"
        raise ValueError(msg)
    # Invalidate the old automatic origin before any replacement. An interruption
    # must never classify a newly explicit (even identical) plan as automatic.
    origin.unlink(missing_ok=True)
    _write_private_json(target, value)
    if implicit:
        _write_private_json(
            origin,
            {
                "version": 1,
                "kind": "local-review-implicit-plan-origin",
                "plan_sha256": plan_digest(value),
                "subject": subject,
            },
        )


def _implicit_origin(target: Path, manifest: dict[str, Any]) -> bool:
    origin = _origin_path(target)
    if origin.is_symlink() or not origin.is_file():
        return False
    try:
        value = json.loads(origin.read_text())
    except (json.JSONDecodeError, UnicodeError):
        return False
    return bool(
        isinstance(value, dict)
        and type(value.get("version")) is int
        and value
        == {
            "version": 1,
            "kind": "local-review-implicit-plan-origin",
            "plan_sha256": plan_digest(manifest),
            "subject": manifest.get("subject"),
        }
    )


def _automatic_plan(root: Path, subject: dict[str, str], provider_module: Any) -> dict[str, Any]:
    suggested = propose(root, subject, provider_module)
    if len(suggested["chunks"]) != 1:
        msg = (
            "An explicit bounded author review plan must be installed for this candidate: "
            "agent_tools/mcp_tool.sh git-workflow review-plan "
            "--review-plan /tmp/author-review-plan.json"
        )
        raise ValueError(msg)
    _persist_plan(root, subject, suggested, implicit=True)
    return suggested


def _refresh_implicit_plan(
    root: Path,
    subject: dict[str, str],
    provider_module: Any,
    target: Path,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    previous = manifest.get("subject")
    if (
        not isinstance(previous, dict)
        or not _implicit_origin(target, manifest)
        or any(previous.get(key) != subject[key] for key in SUBJECT_KEYS if key != "base")
    ):
        msg = (
            "Stored explicit author review plan has a stale candidate binding; reassign and "
            "install it with agent_tools/mcp_tool.sh git-workflow review-plan "
            "--review-plan /tmp/author-review-plan.json"
        )
        raise ValueError(msg)
    # Check the old immutable binding and original automatic shape before trusting
    # the marker; neither a custom assignment nor a changed plan may be refreshed.
    validate(root, previous, manifest, provider_module)
    if manifest != propose(root, previous, provider_module):
        msg = "Stored automatic review plan was modified; install an explicit author review plan"
        raise ValueError(msg)
    files = _files(provider_module, root, subject, target)
    try:
        files.git("merge-base", "--is-ancestor", previous["base"], subject["base"])
        files.git("merge-base", "--is-ancestor", subject["base"], subject["head"])
    except subprocess.SubprocessError as exc:
        msg = (
            "Automatic review plan cannot follow a non-advancing base; "
            "install an explicit author review plan"
        )
        raise ValueError(msg) from exc
    return _automatic_plan(root, subject, provider_module)


def load(root: Path, subject: dict[str, str], provider_module: Any) -> dict[str, Any]:
    target = _plan_path(root, subject)
    if target.is_symlink():
        msg = "Unsafe private review-plan file"
        raise ValueError(msg)
    if not target.exists():
        return _automatic_plan(root, subject, provider_module)
    if not target.is_file():
        msg = "Unsafe private review-plan file"
        raise ValueError(msg)
    manifest = json.loads(target.read_text())
    if isinstance(manifest, dict) and manifest.get("subject") != subject:
        return _refresh_implicit_plan(root, subject, provider_module, target, manifest)
    return validate(root, subject, manifest, provider_module)


def installed_digest(root: Path, subject: dict[str, str]) -> str:
    """Bind approval proof to the current private plan without rereading Git diffs."""
    target = _plan_path(root, subject)
    if target.is_symlink() or not target.is_file():
        msg = "A safe current installed review plan is required to validate approval proof"
        raise ValueError(msg)
    try:
        manifest = json.loads(target.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeError) as exc:
        msg = "Invalid current installed review plan"
        raise ValueError(msg) from exc
    return plan_digest(validate_manifest(subject, manifest))


def _events(ledger: Path, inventory_paths: list[str]) -> list[dict[str, Any]]:
    if ledger.is_symlink() or not ledger.is_file():
        msg = "Missing or unsafe real reviewer read ledger"
        raise ValueError(msg)
    events = [json.loads(line) for line in ledger.read_text().splitlines()]
    if not all(isinstance(event, dict) for event in events) or not any(
        event.get("kind") == "inventory" and event.get("paths") == inventory_paths
        for event in events
    ):
        msg = "Reviewer did not enumerate the actual complete immutable inventory"
        raise ValueError(msg)
    return events


def _verify_events(
    provider_module: Any,
    root: Path,
    subject: dict[str, str],
    paths: list[str],
    events: list[dict[str, Any]],
) -> None:
    with tempfile.TemporaryDirectory(prefix="atk-review-coverage-") as temporary:
        ledger = Path(temporary) / "derived-verification.jsonl"
        files = _files(provider_module, root, subject, ledger)
        files.changed = paths
        expected = {path: _diff(files, path) for path in paths}
        for event in events:
            if event.get("kind") == "diff" and event.get("path") in expected:
                total = len(expected[event["path"]])
                if (
                    type(event.get("offset")) is not int
                    or type(event.get("end")) is not int
                    or not 0 <= event["offset"] <= total
                    or event["end"] != min(total, event["offset"] + provider_module.PAGE)
                ):
                    msg = "Invalid immutable diff-page geometry in reviewer ledger"
                    raise ValueError(msg)
        ledger.write_text(
            "\n".join(
                json.dumps(event) for event in [{"kind": "inventory", "paths": paths}, *events]
            )
            + "\n"
        )
        provider_module.complete_coverage(files)


def scoped_coverage(  # noqa: PLR0913 - preserve separate trusted subject, assignment and read ledger.
    provider_module: Any,
    root: Path,
    subject: dict[str, str],
    manifest: dict[str, Any],
    chunk_id: str,
    ledger: Path,
) -> None:
    """Verify assigned pages through the original trusted validator, never the real ledger."""
    value = validate_manifest(subject, manifest)
    inventory_paths = [item["path"] for item in value["inventory"]]
    files = _files(provider_module, root, subject, ledger)
    if files.changed != inventory_paths:
        msg = "Review plan inventory does not match the actual complete immutable patch"
        raise ValueError(msg)
    chunks = {chunk["id"]: chunk for chunk in value["chunks"]}
    if chunk_id not in chunks:
        msg = "Unknown assigned review chunk"
        raise ValueError(msg)
    events = _events(ledger, files.changed)
    _verify_events(provider_module, root, subject, chunks[chunk_id]["paths"], events)


def aggregate_coverage(
    provider_module: Any,
    root: Path,
    subject: dict[str, str],
    manifest: dict[str, Any],
    ledgers: Mapping[str, Path],
) -> None:
    """Require every assignment and the original validator's complete whole-patch coverage."""
    value = validate(root, subject, manifest, provider_module)
    if set(ledgers) != {chunk["id"] for chunk in value["chunks"]}:
        msg = "A complete review wave requires every component and integration ledger"
        raise ValueError(msg)
    if len({path.resolve() for path in ledgers.values()}) != len(ledgers):
        msg = "Independent review chunks require distinct real read ledgers"
        raise ValueError(msg)
    paths = [item["path"] for item in value["inventory"]]
    events = []
    for chunk in value["chunks"]:
        ledger = ledgers[chunk["id"]]
        scoped_coverage(provider_module, root, subject, value, chunk["id"], ledger)
        events.extend(_events(ledger, paths))
    _verify_events(provider_module, root, subject, paths, events)
