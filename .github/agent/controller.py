"""Trusted, metadata-only GitHub controller. Never execute code from a PR here."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import sqlite3
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal, overload
from urllib.parse import quote

REVIEW = "agent-review"
WRITER = "agent:writing"
STATE_TITLE = "GitHub agent integration monitor"
REPAIR_BRANCH = "codex/integration-repair-"
SHA = re.compile(r"[0-9a-f]{40}")
IDENTITIES: dict[tuple[str, str, str], bool] = {}
MODULES: dict[str, Any] = {}


def trusted_module(name: str) -> Any:
    if name not in MODULES:
        spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + ".py"))
        if spec is None or spec.loader is None:
            raise RuntimeError("Trusted controller module is unavailable.")
        value = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(value)
        MODULES[name] = value
    return MODULES[name]


def task_metadata(gh: GitHub, pr: dict[str, Any]) -> dict[str, Any]:
    proto = trusted_module("task_context")
    body = pr.get("body") or ""
    head = pr["head"]["sha"]
    if proto.MARKER not in body and pr["user"].get("type") == "Bot":
        # Older App repairs inherit the already-authorized standing repair task.
        # The immutable signed head is part of this legacy task identity.
        value = proto.repair_context(head, "legacy-repair:" + head)
        actor = value["producer_id"]
    else:
        value = proto.parse(body)
        if value["source"] == "standing-integration-repair" and pr["user"].get("type") != "Bot":
            raise ValueError("Only the trusted App can inherit the standing repair authority.")
        directory = os.environ.get("AGENT_CONTROL_DIR")
        if not directory:
            raise RuntimeError("Trusted signature checkout is unavailable.")
        message = command("git", "log", "-1", "--format=%B", head, cwd=Path(directory))
        actor = proto.verify_commit(value, message)
        verify_task_images(value, Path(directory), head)
    branch = pr["head"].get("ref", "")
    if branch.startswith("codex/telegram-feature-") and not trusted_module("review_control").enrolled_feature(
            gh.repository, branch, proto.digest(value)):
        raise ValueError("Telegram feature context does not match the host-owned request.")
    return {"user_task": value, "task_digest": proto.digest(value), "producer_id": actor}


def rejection_check(gh: GitHub, pr: dict[str, Any], base: str, reason: str,
                    changes: str) -> None:
    gh.api("check-runs", "POST", {"name": REVIEW, "head_sha": pr["head"]["sha"],
        "external_id": base, "status": "completed", "conclusion": "failure",
        "output": {"title": "Reviewer rejected; awaiting user", "summary": reason[:60000]}})
    trusted_module("review_control").pause(gh, pr, reason, changes)


def verify_review_execution(task: dict[str, Any], receipt: dict[str, Any]) -> None:
    expected = {key: task[key] for key in ("head", "base", "task_digest", "producer_id")}
    identity = receipt.get("reviewer_id")
    if (task["kind"] != "review"
            or any(receipt.get(key) != value for key, value in expected.items())
            or receipt.get("kind") != "review" or receipt.get("mode") != "read-only"
            or not isinstance(identity, str) or not identity.startswith("reviewer:")
            or identity == task["producer_id"]):
        raise RuntimeError("A separate read-only reviewer session is mandatory.")


def verify_correction_plan(task: dict[str, Any], artifact: Path) -> None:
    receipt = json.loads((artifact / "plan-review.json").read_text())
    plan = (artifact / "approved-plan.md").read_text()
    expected_hash = hashlib.sha256((task["task_digest"] + task["base"] + plan + task["head"]).encode()).hexdigest()
    if (any(receipt.get(key) != task.get(key) for key in
            ("head", "base", "task_digest", "producer_id", "plan_hash"))
            or task.get("plan_hash") != expected_hash or receipt.get("approved") is not True
            or receipt.get("mode") != "read-only"
            or not str(receipt.get("reviewer_id", "")).startswith("plan-reviewer:")
            or receipt["reviewer_id"] == task["producer_id"]):
        raise RuntimeError("Correction requires an independent approved plan for this exact task and head.")


@overload
def command(*args: str, cwd: Path | None = None, data: str | None = None,
            raw_stdout: Literal[False] = False) -> str: ...


@overload
def command(*args: str, cwd: Path | None = None, data: str | None = None,
            raw_stdout: Literal[True]) -> bytes: ...


@overload
def command(*args: str, cwd: Path | None = None, data: str | None = None,
            raw_stdout: bool) -> str | bytes: ...


def command(*args: str, cwd: Path | None = None, data: str | None = None,
            raw_stdout: bool = False) -> str | bytes:
    result = subprocess.run(
        args,
        cwd=cwd,
        input=data.encode("utf-8") if raw_stdout and data is not None else data,
        text=not raw_stdout,
        capture_output=True,
        timeout=180,
        env=dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null"),
    )
    if result.returncode:
        stderr = (result.stderr[:7200].decode("utf-8", errors="replace")[:1800]
                  if raw_stdout else result.stderr[:1800])
        raise RuntimeError(f"{args[0]} {args[1]} failed: {stderr}")
    return result.stdout if raw_stdout else result.stdout.strip()


class GitHub:
    def __init__(self, repository: str) -> None:
        self.repository = repository

    def api(self, path: str, method: str = "GET", data: Any = None) -> Any:
        args = ["gh", "api", f"repos/{self.repository}/{path}", "--method", method]
        if data is not None:
            args.extend(["--input", "-"])
        text = command(*args, data=json.dumps(data) if data is not None else None)
        return json.loads(text) if text else None

    def pages(self, path: str, key: str | None = None) -> list[dict[str, Any]]:
        pages = json.loads(command("gh", "api", f"repos/{self.repository}/{path}",
                                   "--paginate", "--slurp"))
        return [item for page in pages for item in (page[key] if key else page)]

    def pulls(self) -> list[dict[str, Any]]:
        return self.pages("pulls?state=open&base=dev&per_page=100")

    def pull(self, number: int) -> dict[str, Any]:
        return self.api(f"pulls/{number}")

    def base(self) -> str:
        return self.api("git/ref/heads/dev")["object"]["sha"]

    def checks(self, head: str) -> list[dict[str, Any]]:
        return self.pages(f"commits/{head}/check-runs?filter=latest&per_page=100", "check_runs")

    def comment(self, number: int, body: str) -> None:
        self.api(f"issues/{number}/comments", "POST", {"body": body[:60000]})

    def label(self, number: int, *, acquire: bool) -> None:
        if acquire:
            self.api(f"issues/{number}/labels", "POST", {"labels": [WRITER]})
        else:
            labels = self.api(f"issues/{number}/labels")
            if any(label["name"] == WRITER for label in labels):
                self.api(f"issues/{number}/labels/{WRITER}", "DELETE")


def trusted(pr: dict[str, Any], repository: str) -> bool:
    author = pr.get("user", {})
    owner = repository.split("/")[0]
    app = os.environ.get("AGENT_APP_SLUG", "analytics-toolkit-agent-717959") + "[bot]"
    own = author.get("type") == "User" and author.get("login", "").casefold() == owner.casefold()
    repair = (author.get("type") == "Bot" and author.get("login") == app
              and pr["head"].get("ref", "").startswith(REPAIR_BRANCH))
    return (not pr["draft"] and pr["base"]["ref"] == "dev" and
            pr["head"].get("repo") is not None and
            pr["head"]["repo"]["full_name"] == repository and pr["state"] == "open"
            and (own or repair or (author.get("type") == "Bot" and author.get("login") == app
                 and trusted_module("review_control").enrolled_feature(repository, pr["head"].get("ref", "")))))


def signed_head(repository: str, head: str) -> bool:
    """Verify the immutable tree using host-owned keys, never a PR's key registry."""
    allowed = os.environ.get("AGENT_ALLOWED_SIGNERS")
    directory = os.environ.get("AGENT_CONTROL_DIR")
    if not allowed or not directory or SHA.fullmatch(head) is None:
        return False
    try:
        registry = Path(allowed).read_bytes()
        identity = (repository, head, hashlib.sha256(registry).hexdigest())
        if identity not in IDENTITIES:
            work = Path(directory)
            command("git", "fetch", "origin", head, cwd=work)
            raw = command("git", "cat-file", "commit", head, cwd=work)
            if "\ngpgsig -----BEGIN SSH SIGNATURE-----\n" not in raw.split("\n\n", 1)[0] + "\n":
                return False
            command("git", "-c", "gpg.ssh.program=/usr/bin/ssh-keygen", "-c",
                    "gpg.ssh.allowedSignersFile=" + allowed, "verify-commit", head, cwd=work)
            IDENTITIES[identity] = True
        return True
    except (OSError, RuntimeError):
        return False


def policy_path(path: str) -> bool:
    return (path.startswith((".github/agent/", ".codex/")) or path in {
        ".github/workflows/github-agent.yml", ".github/workflows/agent-dispatch.yml",
        ".github/workflows/agent-visual.yml", ".github/workflows/publish.yml",
        ".github/required-workflows.json", "agent_tools/machine_signing.py"})


def authorization_error(gh: GitHub, pr: dict[str, Any], require_policy: bool = True) -> str | None:
    head = pr["head"]["sha"]
    if not trusted(pr, gh.repository) or not signed_head(gh.repository, head):
        return "Only the repository owner's PRs signed by an enrolled machine are automated."
    if not require_policy:
        return None  # Proposals can be inspected/repaired; protected activation still needs approval.
    files = gh.pages(f"pulls/{pr['number']}/files?per_page=100")
    if not any(policy_path(path) for item in files
               for path in (item["filename"], item.get("previous_filename", ""))):
        return None
    owner = gh.repository.split("/")[0].casefold()
    approval = "/agent approve-policy " + head
    comments = gh.pages(f"issues/{pr['number']}/comments?per_page=100")
    controls = trusted_module("review_control")
    declined = controls.veto(gh.repository, pr["number"], head)
    if any(comment["user"].get("type") == "User"
           and comment["user"]["login"].casefold() == owner
           and comment["body"].strip() == approval
           and (not declined or datetime.fromisoformat(comment.get("created_at", "1970-01-01T00:00:00Z")
                 .replace("Z", "+00:00")).timestamp() > declined) for comment in comments):
        if declined:
            controls.clear_owner_decline(gh, pr["number"], head)
        return None
    if (os.environ.get("AGENT_STATE_DIR")
            and trusted_module("review_control").approved(gh.repository, pr["number"], head)):
        return None
    return "Policy changes require the owner's exact-head approval comment: `" + approval + "`."


def signing_options() -> list[str]:
    key = os.environ.get("AGENT_SIGNING_KEY")
    if not key:
        raise RuntimeError("Private controller signing key is not configured.")
    return ["-c", "gpg.format=ssh", "-c", "gpg.ssh.program=/usr/bin/ssh-keygen",
            "-c", "user.signingkey=" + key, "-c", "commit.gpgsign=true"]


def review_check(checks: list[dict[str, Any]], base: str, app_id: int) -> dict[str, Any] | None:
    return next((check for check in checks if check["name"] == REVIEW
                 and check.get("app", {}).get("id") == app_id
                 and check.get("external_id") == base), None)


def fast_check(checks: list[dict[str, Any]]) -> dict[str, Any] | None:
    return next((check for check in checks if check["name"] == "fast-checks"
                 and check.get("app", {}).get("slug") == "github-actions"), None)


def ready(pr: dict[str, Any], checks: list[dict[str, Any]], base: str, app_id: int) -> bool:
    review, fast = review_check(checks, base, app_id), fast_check(checks)
    return (pr.get("mergeable") is True and not any(label["name"] in {WRITER, "agent:needs-user"} for label in pr["labels"])
            and review is not None and review["conclusion"] == "success"
            and fast is not None and fast["conclusion"] == "success")


def monitor(gh: GitHub) -> tuple[int, dict[str, Any]]:
    cached = _stored_state()
    if cached is not None:
        return cached["issue"], cached
    issues = gh.pages("issues?state=open&labels=agent:state&per_page=100")
    issue = next((item for item in issues if item["title"] == STATE_TITLE), None)
    if issue is None:
        state: dict[str, Any] = {
            "started": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(), "runs": {}
        }
        issue = gh.api("issues", "POST", {"title": STATE_TITLE, "labels": ["agent:state"],
                                          "body": json.dumps(state, indent=2)})
    state = json.loads(issue["body"])
    state.setdefault("runs", {})
    state["issue"] = issue["number"]
    save_monitor(gh, state)
    return issue["number"], state


def dispatch(gh: GitHub, operation: str, value: Any) -> None:
    issue, _ = monitor(gh)
    gh.comment(issue, "<!-- github-agent-dispatch\n"
               + json.dumps({"operation": operation, "value": value}) + "\n-->")


def _stored_state() -> dict[str, Any] | None:
    directory = os.environ.get("AGENT_STATE_DIR")
    if not directory:
        return None
    path = Path(directory)
    path.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path / "monitor.sqlite3") as connection:
        connection.execute("CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY, body TEXT)")
        row = connection.execute("SELECT body FROM state WHERE id=1").fetchone()
    return json.loads(row[0]) if row else None


def save_monitor(gh: GitHub, state: dict[str, Any]) -> None:
    # Persist full history privately, publish only bounded unresolved status.
    cutoff = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
    state["runs"] = {key: run for key, run in state["runs"].items()
                     if run.get("needs") or run["status"] != "completed"
                     or run.get("updated", cutoff) >= cutoff}
    unresolved = {key: value for key, value in state["runs"].items() if value.get("needs")}
    body = json.dumps({"started": state["started"], "tracked_runs": len(state["runs"]),
                       "outstanding_failures": len(unresolved),
                       "runs": dict(list(unresolved.items())[-15:])}, indent=2)
    if body != state.get("published"):
        gh.api(f"issues/{state['issue']}", "PATCH", {"body": body[:60000]})
        state["published"] = body
    directory = os.environ.get("AGENT_STATE_DIR")
    if directory:
        _stored_state()
        with sqlite3.connect(Path(directory) / "monitor.sqlite3") as connection:
            connection.execute("INSERT OR REPLACE INTO state VALUES (1, ?)", (json.dumps(state),))


def _record_run(gh: GitHub, state: dict[str, Any], run: dict[str, Any]) -> None:
    previous = state["runs"].setdefault(str(run["id"]), {})
    unchanged = (previous.get("status"), previous.get("conclusion"), previous.get("attempt")) == (
        run["status"], run["conclusion"], run["run_attempt"])
    candidate = re.fullmatch(r"agent integration ([0-9a-f]{40}) PR (\d+)", run.get("display_title", ""))
    tested_sha = candidate.group(1) if candidate else run["head_sha"]
    if candidate:
        previous["candidate"] = tested_sha
        previous["pull_number"] = int(candidate.group(2))
        pr = gh.pull(previous["pull_number"])
        previous["merged"] = bool(pr.get("merged"))
        previous["closed_unmerged"] = pr.get("state") == "closed" and not previous["merged"]
        if previous["merged"]:
            tested_sha = pr["merge_commit_sha"]
    previous.update(sha=tested_sha, status=run["status"], conclusion=run["conclusion"],
                    url=run["html_url"], event=run["event"], attempt=run["run_attempt"],
                    updated=run["updated_at"])
    if previous.get("closed_unmerged"):
        previous["needs"] = []
    if run["status"] == "completed" and not unchanged and not previous.get("closed_unmerged"):
        jobs = gh.pages(f"actions/runs/{run['id']}/jobs?filter=latest&per_page=100", "jobs")
        grouped: dict[str, list[Any]] = {}
        for job in jobs:
            grouped.setdefault(job["name"], []).append(job["conclusion"])
        previous["green"] = sorted(name for name, results in grouped.items()
                                   if all(result == "success" for result in results))
        previous["needs"] = sorted(name for name, results in grouped.items()
                                   if any(result not in {"success", "skipped"} for result in results)
                                   or "success" in results and "skipped" in results)
        if run["conclusion"] != "success" and not previous["needs"]:
            previous["needs"] = ["workflow completion"]


def _resolve_runs(gh: GitHub, state: dict[str, Any]) -> None:
    ancestry: dict[tuple[str, str], bool] = {}
    for failure in state["runs"].values():
        for green in state["runs"].values():
            if green["status"] != "completed" or not green.get("merged", True):
                continue
            common = set(failure.get("needs", [])) & set(green.get("green", []))
            if not common or green.get("updated", "") < failure.get("updated", ""):
                continue
            pair = (failure["sha"], green["sha"])
            if pair not in ancestry:
                ancestry[pair] = pair[0] == pair[1] or gh.api(
                    f"compare/{pair[0]}...{pair[1]}")["status"] in {"ahead", "identical"}
            if ancestry[pair]:
                failure["needs"] = sorted(set(failure["needs"]) - common)
                failure["verified_by"] = green["url"]


def _integration_tasks(gh: GitHub, state: dict[str, Any], base: str,
                       pulls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    failures = [int(key) for key, run in state["runs"].items()
                if run.get("needs") and run.get("merged", True)]
    if not failures or any(pr["head"]["ref"].startswith(REPAIR_BRANCH) for pr in pulls):
        return []
    for key, run in state["runs"].items():
        if int(key) not in failures:
            continue
        if "workflow completion" in run.get("needs", []) and time.time() >= run.get("retry_after", 0):
            dispatch(gh, "rerun", int(key))
            run["retry_after"] = time.time() + 1800
            return []
    current = [r for r in state["runs"].values() if r["sha"] == base]
    if any(r["status"] != "completed" for r in current):
        return []
    # Historical failures are evidence for a repair PR, not a reason to run
    # another matrix on every new dev SHA. The final repair candidate is tested
    # before its merge by the same path as ordinary PRs.
    proto = trusted_module("task_context")
    producer = "standing-repair:" + base
    context = proto.repair_context(base, producer)
    task = {"kind": "repair", "number": 0, "head": base, "base": base,
             "branch": REPAIR_BRANCH + base[:12], "runs": failures,
             "producer_id": producer, "user_task": context, "task_digest": proto.digest(context)}
    evidence = trusted_module("repair_evidence")
    task["failure_key"] = evidence.fingerprint([
        {"run": run, "needs": sorted(state["runs"][str(run)].get("needs", []))}
        for run in sorted(failures)])
    try:
        inventory = evidence.inventory(gh, failures)
    except RuntimeError as exc:
        if not (str(exc).startswith("gh api failed:")
                and re.search(r"\(HTTP 5[0-9]{2}\)", str(exc))):
            raise
        # Defer only repair discovery; preserve PR tasks and monitor retry state.
        return []
    task["evidence_key"] = evidence.fingerprint(inventory)
    return [] if evidence.blocked(task) else [task]


def discover(gh: GitHub, app_id: int) -> dict[str, Any]:
    issue, state = monitor(gh)
    next_cursor = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    # Cursor overlaps by a minute; active runs are always polled by immutable ID.
    runs = gh.pages("actions/workflows/sql-integration.yml/runs?branch=dev&per_page=100&created=%3E%3D"
                    + quote(state.get("cursor", state["started"]), safe=""), "workflow_runs")
    for run in runs:
        _record_run(gh, state, run)
    for key, run in list(state["runs"].items()):
        if run["status"] != "completed" or run.get("needs") or run.get("pull_number") and not run.get("merged") and not run.get("closed_unmerged"):
            try:
                metadata = gh.api(f"actions/runs/{key}")
            except RuntimeError as exc:
                if not (str(exc).startswith("gh api failed:")
                        and re.search(r"\(HTTP 5[0-9]{2}\)", str(exc))):
                    raise
                continue
            _record_run(gh, state, metadata)
    state["cursor"] = next_cursor
    _resolve_runs(gh, state)
    base = gh.base()
    pulls = gh.pulls()
    tasks = []
    for pr in sorted(pulls, key=lambda item: item["number"]):
        if not trusted(pr, gh.repository):
            continue
        author = pr["user"]
        if author["type"] != "Bot":
            permission = gh.api(f"collaborators/{author['login']}/permission")["permission"]
            if permission not in {"admin", "write", "maintain"}:
                continue
        elif not (pr["head"]["ref"].startswith(REPAIR_BRANCH)
                  or trusted_module("review_control").enrolled_feature(gh.repository, pr["head"]["ref"])):
            continue
        pr = gh.pull(pr["number"])
        head = pr["head"]["sha"]
        if any(label["name"] == WRITER for label in pr.get("labels", [])):
            continue
        checks = gh.checks(head)
        existing = review_check(checks, base, app_id)
        if not signed_head(gh.repository, head):
            if existing is None or existing.get("output", {}).get("title") != "Authorization required":
                gh.api("check-runs", "POST", {"name": REVIEW, "head_sha": head,
                    "external_id": base, "status": "completed", "conclusion": "action_required",
                    "output": {"title": "Authorization required", "summary":
                               "Only enrolled-machine signed heads are automated."}})
            continue
        controls = trusted_module("review_control")
        if controls.paused(gh, pr):
            continue
        try:
            metadata = task_metadata(gh, pr)
        except (RuntimeError, ValueError, OSError) as exc:
            if signed_head(gh.repository, head):
                rejection_check(gh, pr, base, str(exc), pr.get("title", "Missing user task context"))
            continue
        denial = authorization_error(gh, pr, False)
        if denial:
            if existing is None or existing.get("output", {}).get("summary") != denial:
                gh.api("check-runs", "POST", {"name": REVIEW, "head_sha": head,
                    "external_id": base, "status": "completed", "conclusion": "action_required",
                    "output": {"title": "Authorization required", "summary": denial}})
            continue
        fast = fast_check(checks)
        comparison = gh.api(f"compare/{base}...{head}")
        kind = "conflict" if pr.get("mergeable") is False or comparison["behind_by"] > 0 else "review"
        correction = controls.correction(gh.repository, pr["number"], head, metadata["task_digest"])
        if existing and existing.get("conclusion") == "failure":
            correction = correction or {"reason": existing.get("output", {}).get("summary", "Reviewer requested changes."),
                                        "changes": pr.get("title", "PR changes")}
        if fast and fast["conclusion"] in {"failure", "cancelled", "timed_out"}:
            reason = "Fast CI failed; correct the failing checks: " + fast.get("html_url", "")
            correction = correction or {"reason": reason, "changes": pr.get("title", "PR changes")}
        if correction:
            kind = "correction"
        elif kind == "review":
            files = gh.pages(f"pulls/{pr['number']}/files?per_page=100")
            if any(item["filename"].startswith("agent_tools/changelog/")
                   and item.get("status") != "removed" for item in files):
                kind = "metadata"
        if (kind == "review" and existing and existing["status"] == "completed"
                and existing.get("output", {}).get("title") != "Authorization required"):
            if existing.get("conclusion") == "success" and authorization_error(gh, pr):
                paths = sorted({path for item in files for path in (item["filename"], item.get("previous_filename", ""))
                                if policy_path(path)})
                controls.approval_notice(gh, pr, paths, metadata["user_task"])
            continue
        tasks.append({"kind": kind, "number": pr["number"], "head": head,
                      "base": base, "branch": pr["head"]["ref"], **metadata,
                      **({"rejection": correction} if correction else {})})
    tasks = _integration_tasks(gh, state, base, pulls) + tasks
    save_monitor(gh, state)
    return {"include": tasks}


def prepare(gh: GitHub, task: dict[str, Any], work: Path, control: Path) -> None:
    kind = task["kind"]
    if task["number"]:
        pr = gh.pull(task["number"])
        if pr["head"]["sha"] != task["head"] or not trusted(pr, gh.repository):
            raise RuntimeError("PR changed after discovery; reconcile again.")
        denial = authorization_error(gh, pr, False)
        if denial:
            raise RuntimeError(denial)
        if trusted_module("review_control").paused(gh, pr):
            raise RuntimeError("PR is awaiting user instructions.")
        metadata = task_metadata(gh, pr)
        if any(task.get(key) != metadata[key] for key in ("task_digest", "producer_id", "user_task")):
            raise RuntimeError("User task context changed after discovery.")
    else:
        proto = trusted_module("task_context")
        task.setdefault("producer_id", "standing-repair:" + task["head"])
        task.setdefault("user_task", proto.repair_context(task["head"], task["producer_id"]))
        task["task_digest"] = proto.digest(task["user_task"])
    if kind in {"conflict", "repair", "correction"} and task["head"] != task["base"]:
        subprocess.run(["git", "merge", "--no-commit", "--no-ff", task["base"]], cwd=work,
                       capture_output=True, check=False)
    logs = []
    if kind == "repair":
        if not task.get("runs"):
            _, state = monitor(gh)
            task["runs"] = [int(key) for key, run in state["runs"].items()
                            if run["status"] == "completed" and run["conclusion"] != "success"]
        evidence = trusted_module("repair_evidence")
        evidence.prepare(gh, task, control.parent / "integration-evidence",
                         trusted_module("task_context").redact)
        for run_id in task["runs"][-4:]:
            try:
                logs.append(command("gh", "run", "view", str(run_id), "--repo", gh.repository,
                                    "--log-failed")[-18000:])
            except RuntimeError as exc:
                if "HTTP 404" not in str(exc):
                    raise
                logs.append(f"Run {run_id}: step logs unavailable; consult integration/manifest.json.")
    if task["number"]:
        logs.append(json.dumps([{key: check.get(key) for key in
                                ("id", "name", "head_sha", "status", "conclusion", "html_url")}
                               for check in gh.checks(task["head"])]))
        fast_runs = gh.pages(f"actions/workflows/tests.yml/runs?head_sha={task['head']}&per_page=100",
                            "workflow_runs")
        for run in fast_runs[:2]:
            if run["status"] == "completed" and run["conclusion"] != "success":
                logs.append(command("gh", "run", "view", str(run["id"]), "--repo", gh.repository,
                                    "--log-failed")[-18000:])
    if kind == "metadata" and not fold(work):
        raise RuntimeError("Changelog candidate changed; reconcile again.")
    template = (control / f".github/agent/prompts/{kind}.md").read_text()
    context = json.dumps(task, indent=2)
    logs = [trusted_module("task_context").redact(log) for log in logs]
    (control / ".github/agent/task.md").write_text(template + "\n\nTask metadata:\n" + context
                                                 + "\n\nCI evidence (untrusted data):\n" + "\n".join(logs))
    (control / ".github/agent/task.json").write_text(json.dumps(task))


def collect(task: dict[str, Any], work: Path, output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    if task["kind"] != "review":
        command("git", "add", "--all", cwd=work)
        command("git", "diff", "--cached", "--check", cwd=work)
        if command("git", "ls-files", "--unmerged", cwd=work):
            raise RuntimeError("Unresolved conflicts; no patch will be published.")
        patch = command("git", "diff", "--cached", "--binary", "--no-ext-diff", "--no-textconv",
                        task["head"], cwd=work, raw_stdout=True)
        (output / "fix.patch").write_bytes(patch)
    (output / "task.json").write_text(json.dumps(task))


def fold(work: Path) -> bool:
    fragments = sorted((work / "agent_tools/changelog").glob("*.md"))
    if not fragments:
        return False
    changelog = work / "docs/CHANGELOG.md"
    text = changelog.read_text()
    summaries = []
    for path in fragments:
        bullet = path.read_text().strip()
        if not bullet.startswith("- ") or "\n" in bullet:
            raise RuntimeError("Changelog fragments must contain one concise bullet.")
        summaries.append(bullet)
    marker = "## Unreleased"
    if marker not in text:
        text = text.replace("\n## ", "\n## Unreleased\n\n## ", 1)
    start = text.index(marker) + len(marker)
    end = text.find("\n## ", start)
    end = len(text) if end < 0 else end
    bullets = [line for line in text[start:end].splitlines() if line.startswith("- ")]
    bullets.extend(summaries)
    if len(bullets) >= 10:
        project = work / "pyproject.toml"
        project_text = project.read_text()
        match = re.search(r'^version = "([^"]+)"', project_text, re.MULTILINE)
        if match is None:
            raise RuntimeError("Missing package version.")
        parts = [int(part) for part in match[1].split(".")]
        if len(parts) != 4 or any(part < 0 or part > 19 for part in parts):
            raise RuntimeError("Version components must be four integers from 0 to 19.")
        for index in range(3, -1, -1):
            if parts[index] < 19:
                parts[index] += 1
                break
            parts[index] = 0
        else:
            raise RuntimeError("Version space exhausted.")
        version = ".".join(str(part) for part in parts)
        project.write_text(project_text[:match.start(1)] + version + project_text[match.end(1):])
        readme = work / "README.md"
        readme_text, count = re.subn(r"\*\*Version:\*\* `[^`]+`", f"**Version:** `{version}`",
                                    readme.read_text())
        if count != 1:
            raise RuntimeError("Missing README version marker.")
        readme.write_text(readme_text)
        section = f"\n\n## {version} - {datetime.now(timezone.utc).date()}\n\n"
    else:
        section = "\n\n"
    changelog.write_text(text[:start] + section + "\n".join(bullets) + "\n" + text[end:])
    for path in fragments:
        path.unlink()
    return True


def visual_required(paths: list[str]) -> bool:
    return any(path.startswith(("analytics_toolkit/sql_explorer/", "agent_tools/sql_explorer_visual",
                                "visual-tests/sql_explorer/", "tests/sql_explorer/")) for path in paths)


def candidate(gh: GitHub, task: dict[str, Any], artifact: Path, work: Path) -> str:
    patch = artifact / "fix.patch"
    if patch.read_bytes().strip():
        command("git", "apply", "--index", str(patch.resolve()), cwd=work)
    tree = command("git", "write-tree", cwd=work)
    commit = command("git", *signing_options(), "commit-tree", "-S", tree, "-p", task["head"],
                     "-m", "Temporary visual candidate", cwd=work)
    branch = "agent-visual/" + commit
    command("git", "push", "origin", f"{commit}:refs/heads/{branch}", cwd=work)
    dispatch(gh, "visual", commit)
    return commit


def verify_visual(artifact: Path, patch: Path, task: dict[str, Any]) -> None:
    receipt = json.loads((artifact / "visual-receipt.json").read_text())
    if receipt["task"] != task or receipt["patch_sha256"] != hashlib.sha256(patch.read_bytes()).hexdigest():
        raise RuntimeError("Stale visual receipt")
    session = receipt["capture"]
    if session["capture"]["status"] != "pass" or not session["capture"]["workspace_removed"]:
        raise RuntimeError("Incomplete visual capture")
    if set(receipt["scenes"]) != set(session["scenes"]) or not receipt["scenes"]:
        raise RuntimeError("Partial visual review")
    if any(scene["verdict"] != "pass" or not scene["notes"] for scene in receipt["scenes"].values()):
        raise RuntimeError("Non-green visual review")


def conflict_entry_metadata(raw: bytes, path: str, *, staged: bool) -> tuple[bytes, bytes]:
    records = raw.split(b"\0")
    if len(records) != 2 or records[-1] != b"":
        raise RuntimeError("Malformed conflict path entries.")
    metadata, separator, name = records[0].partition(b"\t")
    fields = metadata.split(b" ")
    if (
        not separator or name != path.encode("utf-8") or len(fields) != 3
        or fields[0] not in {b"100644", b"100755", b"120000"}
    ):
        raise RuntimeError("Malformed conflict path metadata.")
    if staged:
        oid = fields[1]
        if fields[2] != b"0":
            raise RuntimeError("Unresolved conflict index entry.")
    else:
        oid = fields[2]
        if fields[1] != b"blob":
            raise RuntimeError("Conflict path is not a blob.")
    return fields[0], oid


def conflict_path_entry(work: Path, path: str, revision: str | None) -> tuple[bytes, bytes] | None:
    if revision is None:
        raw = command("git", "ls-files", "--stage", "-z", "--",
                      ":(literal)" + path, cwd=work, raw_stdout=True)
    else:
        raw = command("git", "ls-tree", "--full-tree", "-z", revision, "--",
                      path, cwd=work, raw_stdout=True)
    if not raw:
        return None  # Successful lookup proves absence; command failures raise.
    mode, oid = conflict_entry_metadata(raw, path, staged=revision is None)
    object_id = oid.decode("ascii")
    if (SHA.fullmatch(object_id) is None
            or command("git", "cat-file", "-t", object_id, cwd=work) != "blob"):
        raise RuntimeError("Unavailable conflict blob.")
    return mode, oid


def verify_task_images(value: dict[str, Any], work: Path, revision: str) -> None:
    """Verify owner-approved PNG bytes from an immutable Git tree, never symlinks."""
    trusted_module("task_context").validate(value)
    for image in value.get("images", []):
        raw = command("git", "ls-tree", "--full-tree", "-z", revision, "--",
                      image["path"], cwd=work, raw_stdout=True)
        if not raw:
            raise ValueError("Image evidence is missing from the candidate tree.")
        mode, oid = conflict_entry_metadata(raw, image["path"], staged=False)
        if mode not in {b"100644", b"100755"}:
            raise ValueError("Image evidence must be a regular Git blob.")
        object_id = oid.decode("ascii")
        if (SHA.fullmatch(object_id) is None
                or command("git", "cat-file", "-t", object_id, cwd=work) != "blob"):
            raise ValueError("Image evidence object is unavailable.")
        size = int(command("git", "cat-file", "-s", object_id, cwd=work))
        if not 1 <= size <= 524288:
            raise ValueError("Image evidence size is outside the publication limit.")
        data = command("git", "cat-file", "blob", object_id, cwd=work, raw_stdout=True)
        if len(data) != size or not data.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("Image evidence must contain the declared PNG bytes.")
        if hashlib.sha256(data).hexdigest() != image["sha256"]:
            raise ValueError("Image evidence does not match its signed task digest.")


def inherited_controller_path(task: dict[str, Any], work: Path, path: str) -> bool:
    """Prove a conflict change came solely from immutable dev, including absence."""
    for revision in (task["head"], task["base"]):
        if SHA.fullmatch(revision) is None or command(
                "git", "cat-file", "-t", revision, cwd=work) != "commit":
            raise RuntimeError("Unavailable immutable conflict history.")
    ancestors = command("git", "merge-base", "--all", task["head"], task["base"],
                        cwd=work).splitlines()
    if len(ancestors) != 1 or SHA.fullmatch(ancestors[0]) is None:
        raise RuntimeError("Conflict inheritance requires one actual merge base.")
    ancestor = ancestors[0]
    if command("git", "cat-file", "-t", ancestor, cwd=work) != "commit":
        raise RuntimeError("Unavailable conflict ancestor.")
    original, head, base, staged = (
        conflict_path_entry(work, path, revision)
        for revision in (ancestor, task["head"], task["base"], None))
    return original == head and base != original and staged == base


def publish(gh: GitHub, task: dict[str, Any], artifact: Path, work: Path) -> dict[str, Any] | None:
    number = task["number"]
    try:
        if not (artifact / "task.json").exists():
            raise RuntimeError("Model job failed; inspect its Actions run before retrying.")
        if json.loads((artifact / "task.json").read_text()) != task:
            raise RuntimeError("Artifact task identity does not match the claimed job.")
        if gh.base() != task["base"]:
            raise RuntimeError("Dev changed during execution; discard the stale decision.")
        if number:
            pr = gh.pull(number)
            if not trusted(pr, gh.repository) or pr["head"]["sha"] != task["head"]:
                raise RuntimeError("Stale model output; PR changed during execution.")
            denial = authorization_error(gh, pr, False)
            if denial:
                raise RuntimeError(denial)
            if trusted_module("review_control").paused(gh, pr):
                raise RuntimeError("PR is awaiting user instructions.")
            metadata = task_metadata(gh, pr)
            if metadata["task_digest"] != task.get("previous_task_digest", task["task_digest"]):
                raise RuntimeError("Task context changed while the reviewer was running.")
        if task["kind"] == "review":
            receipt = json.loads((artifact / "review-execution.json").read_text())
            verify_review_execution(task, receipt)
            verdict = json.loads((artifact / "verdict.json").read_text())
            if (verdict.get("alignment") not in {"aligned", "not_aligned", "uncertain"}
                    or not isinstance(verdict.get("change_summary"), str)
                    or not verdict["change_summary"].strip()):
                raise RuntimeError("Reviewer must explicitly evaluate alignment with the user task.")
            approved = verdict.get("approved") is True and verdict["alignment"] == "aligned"
            if not isinstance(verdict.get("summary"), str) or not isinstance(verdict.get("findings"), list):
                raise RuntimeError("Malformed review verdict.")
            summary = "What changed: " + verdict.get("change_summary", pr.get("title", "PR changes"))
            summary += "\n\n" + verdict["summary"] + "\n" + "\n".join(str(x) for x in verdict["findings"])
            controls = trusted_module("review_control")
            controls.assess(gh.repository, number, task["head"], task["task_digest"], verdict["change_summary"])
            if (artifact / "visual-receipt.json").exists():
                visual = json.loads((artifact / "visual-receipt.json").read_text())
                if visual["task"] != task or visual["capture"]["candidate"] != task["head"]:
                    raise RuntimeError("Visual review identity mismatch")
                summary += "\nEvery scene reviewed individually: " + visual["url"]
            gh.api("check-runs", "POST", {"name": REVIEW, "head_sha": task["head"],
                "external_id": task["base"], "status": "completed",
                "conclusion": "success" if approved else "failure",
                "output": {"title": "Approved" if approved else "Changes requested",
                           "summary": summary[:60000]}})
            gh.api(f"pulls/{number}/reviews", "POST", {"commit_id": task["head"],
                "event": "COMMENT", "body": summary[:60000]})
            if not approved:
                reason = verdict["summary"] + "\n" + "\n".join(str(x) for x in verdict["findings"])
                controls.reject(gh, pr, task, reason, verdict["change_summary"])
            return
        trusted_module("task_context").require_resolved(task["user_task"])
        if task["kind"] == "correction":
            verify_correction_plan(task, artifact)
        patch = artifact / "fix.patch"
        if task["kind"] == "repair":
            outcome = json.loads((artifact / "verdict.json").read_text())
            if (set(outcome) != {"outcome", "reason", "required_evidence"}
                    or outcome["outcome"] not in {"patch", "blocked", "no_change"}
                    or not isinstance(outcome["reason"], str) or not outcome["reason"].strip()
                    or not isinstance(outcome["required_evidence"], list)
                    or any(not isinstance(item, str) for item in outcome["required_evidence"])):
                raise ValueError("Malformed repair outcome")
            has_patch = bool(patch.read_bytes().strip())
            if has_patch != (outcome["outcome"] == "patch"):
                raise ValueError("Repair outcome does not match its patch")
            if not has_patch:
                evidence = trusted_module("repair_evidence")
                if not evidence.blocked(task):
                    issue, _ = monitor(gh)
                    redact = trusted_module("task_context").redact
                    message = ("Integration repair " + outcome["outcome"] + ": "
                               + redact(outcome["reason"])[:4000] + "\nRequired evidence: "
                               + redact("; ".join(outcome["required_evidence"]))[:4000]
                               + "\nRetry when the dev base, failures, or available evidence changes.")
                    trusted_module("review_control").enqueue(
                        "repair-blocked:" + evidence.identity(task), "repair-blocked",
                        {"title": "Integration repair needs evidence", "reason": message,
                         "url": f"https://github.com/{gh.repository}/issues/{issue}"})
                    gh.comment(issue, message)
                    evidence.record(task, outcome)
                return {"status": "blocked"}
        if not patch.read_bytes().strip() and task["kind"] != "conflict":
            raise RuntimeError("No repair produced. Diagnose credentials/infrastructure explicitly.")
        if patch.read_bytes().strip():
            command("git", "apply", "--index", str(patch.resolve()), cwd=work)
        paths = command("git", "diff", "--cached", "--name-only", task["head"], cwd=work).splitlines()
        for path in paths:
            sensitive = path.startswith(".git/") or path in {".connections", ".env"}
            protected = (path.startswith(".github/agent/")
                         or path == ".github/workflows/github-agent.yml")
            if sensitive or (protected and task["kind"] not in {"feature", "correction"}
                             and (task["kind"] != "conflict"
                                  or not inherited_controller_path(task, work, path))):
                raise RuntimeError("Repair modifies controller or sensitive files; maintainer review required.")
        if task["kind"] == "repair" and not any(path.startswith("tests/") and path.endswith(".py")
                                                 for path in paths):
            raise RuntimeError("Integration repair must include a fast regression test.")
        if visual_required(paths):
            verify_visual(artifact, patch, task)
        tree = command("git", "write-tree", cwd=work)
        verify_task_images(task["user_task"], work, tree)
        parents = ["-p", task["head"]]
        if task["kind"] in {"conflict", "repair", "feature", "correction"} and task["base"] != task["head"]:
            parents.extend(["-p", task["base"]])
        proto = trusted_module("task_context")
        message = "GitHub agent: " + task["kind"] + proto.trailers(task["user_task"], task["producer_id"])
        commit = None
        if task["kind"] == "feature" and not number:
            remote = command("git", "ls-remote", "origin", "refs/heads/" + task["branch"], cwd=work)
            if remote:
                existing_head = remote.split()[0]
                command("git", "fetch", "origin", existing_head, cwd=work)
                if command("git", "rev-parse", existing_head + "^{tree}", cwd=work) != tree:
                    raise RuntimeError("Existing feature branch differs; publication recovery requires owner correction.")
                proto.verify_commit(task["user_task"], command("git", "log", "-1", "--format=%B", existing_head, cwd=work))
                verify_task_images(task["user_task"], work, existing_head)
                commit = existing_head
        if commit is None:
            commit = command("git", *signing_options(), "commit-tree", "-S", tree, *parents,
                             "-m", message, cwd=work)
        push = ["git", "push"]
        if number:
            push.append(f"--force-with-lease=refs/heads/{task['branch']}:{task['head']}")
        command(*push, "origin", f"{commit}:refs/heads/{task['branch']}", cwd=work)
        if number:
            prefix = (pr.get("body") or "").split("## User task", 1)[0]
            gh.api(f"pulls/{number}", "PATCH", {"body": prefix + "\n\n" + proto.render(task["user_task"])})
        if not number:
            existing_pr = next((item for item in gh.pulls() if item["head"]["ref"] == task["branch"]), None)
            pr = existing_pr or gh.api("pulls", "POST", {"title": task.get("title", "Repair post-merge integration failures"),
                "head": task["branch"], "base": "dev", "body": ("Owner-requested Telegram feature. " if task["kind"] == "feature" else "Automated integration repair. ")
                + "Includes a fast regression test; requires independent review and fast CI.\n\n"
                + proto.render(task["user_task"])})
            if task["kind"] != "feature":
                gh.comment(pr["number"], "Integration runs: " + ", ".join(str(x) for x in task["runs"]))
        if task["kind"] in {"feature", "correction"}:
            return {"number": pr["number"], "url": pr["html_url"], "head": commit}
        return None
    except (RuntimeError, ValueError, OSError) as exc:
        if number:
            gh.comment(number, "GitHub agent blocker: " + str(exc))
        else:
            issue, _ = monitor(gh)
            gh.comment(issue, "Integration repair blocker: " + str(exc))
        raise
    finally:
        if number and task["kind"] != "review":
            gh.label(number, acquire=False)


def merge(gh: GitHub, app_id: int, work: Path, *,
          pr_numbers: set[int] | None = None,
          active_branches: set[str] | None = None) -> None:
    for item in gh.pulls():
        if pr_numbers is not None and item["number"] not in pr_numbers:
            continue
        pr = gh.pull(item["number"])
        if not trusted(pr, gh.repository):
            continue
        head, base = pr["head"]["sha"], gh.base()
        if authorization_error(gh, pr):
            continue
        if trusted_module("review_control").paused(gh, pr):
            continue
        task_metadata(gh, pr)
        if not ready(pr, gh.checks(head), base, app_id):
            continue
        # Folding/rebasing must already have happened before final review.
        files = gh.pages(f"pulls/{pr['number']}/files?per_page=100")
        if any(item["filename"].startswith("agent_tools/changelog/") and item.get("status") != "removed" for item in files):
            continue
        if gh.base() != base:
            continue
        # Queue once for the final head, including heads produced by conflict fixes.
        # Completion and success remain advisory; monitoring continues after merge.
        _, state = monitor(gh)
        candidate_key = str(pr["number"]) + ":" + head
        if candidate_key not in state.setdefault("candidates", {}):
            dispatch(gh, "integration", {"candidate": head, "number": pr["number"]})
            state["candidates"][candidate_key] = {"head": head, "base": base}
            save_monitor(gh, state)
        # GitHub protection also enforces fast checks; expected head rejects races.
        result = gh.api(f"pulls/{pr['number']}/merge", "PUT", {"sha": head, "merge_method": "squash"})
        if result.get("merged"):
            trusted_module("branch_cleanup").cleanup(
                gh, work, force=True, active_branches=active_branches)


def merge_selected(gh: GitHub, app_id: int, work: Path, *,
                   pr_numbers: set[int], active_branches: set[str]) -> None:
    """Scope the trusted merge loop without weakening any merge gate."""
    merge(gh, app_id, work, pr_numbers=pr_numbers, active_branches=active_branches)


def cleanup_branches(gh: GitHub, *, active_branches: set[str] | None = None) -> dict[str, Any]:
    directory = os.environ.get("AGENT_CONTROL_DIR")
    if not directory:
        raise RuntimeError("Trusted cleanup checkout is unavailable.")
    return trusted_module("branch_cleanup").cleanup(gh, Path(directory), active_branches=active_branches)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["discover", "prepare", "collect", "publish", "merge"])
    args = parser.parse_args()
    gh = GitHub(os.environ["GITHUB_REPOSITORY"])
    work, control, artifact = Path("work"), Path("control"), Path("artifact")
    task = json.loads(os.environ.get("AGENT_TASK", "{}"))
    if args.action == "discover":
        tasks = discover(gh, int(os.environ["AGENT_APP_ID"]))
        with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
            output.write("tasks=" + json.dumps(tasks) + "\n")
            output.write("count=" + str(len(tasks["include"])) + "\n")
    elif args.action == "prepare":
        prepare(gh, task, work, control)
    elif args.action == "collect":
        collect(task, work, artifact)
    elif args.action == "publish":
        publish(gh, task, artifact, work)
    else:
        merge(gh, int(os.environ["AGENT_APP_ID"]), work)


if __name__ == "__main__":
    main()
