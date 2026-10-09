"""Trusted, metadata-only GitHub controller. Never execute code from a PR here."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

REVIEW = "agent-review"
WRITER = "agent:writing"
STATE_TITLE = "GitHub agent integration monitor"
REPAIR_BRANCH = "codex/integration-repair-"
SHA = re.compile(r"[0-9a-f]{40}")
IDENTITIES: dict[tuple[str, str, str], bool] = {}


def command(*args: str, cwd: Path | None = None, data: str | None = None) -> str:
    result = subprocess.run(args, cwd=cwd, input=data, text=True, capture_output=True, timeout=180,
                            env=dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null"))
    if result.returncode:
        raise RuntimeError(f"{args[0]} {args[1]} failed: {result.stderr[:1800]}")
    return result.stdout.strip()


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
            and (own or repair))


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
    if path.startswith("agent_tools/changelog/") and path.endswith(".md"):
        return False
    return (path == "AGENTS.md" or path.endswith("/AGENTS.md") or
            path.startswith(("agent_docs/", "agent_tools/", ".github/", ".codex/",
                             ".agents/", "release_routines/")))


def authorization_error(gh: GitHub, pr: dict[str, Any]) -> str | None:
    head = pr["head"]["sha"]
    if not trusted(pr, gh.repository) or not signed_head(gh.repository, head):
        return "Only the repository owner's PRs signed by an enrolled machine are automated."
    files = gh.pages(f"pulls/{pr['number']}/files?per_page=100")
    if not any(policy_path(path) for item in files
               for path in (item["filename"], item.get("previous_filename", ""))):
        return None
    owner = gh.repository.split("/")[0].casefold()
    approval = "/agent approve-policy " + head
    comments = gh.pages(f"issues/{pr['number']}/comments?per_page=100")
    if any(comment["user"].get("type") == "User"
           and comment["user"]["login"].casefold() == owner
           and comment["body"].strip() == approval for comment in comments):
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
    return (pr.get("mergeable") is True and not any(label["name"] == WRITER for label in pr["labels"])
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
        if previous["merged"]:
            tested_sha = pr["merge_commit_sha"]
    previous.update(sha=tested_sha, status=run["status"], conclusion=run["conclusion"],
                    url=run["html_url"], event=run["event"], attempt=run["run_attempt"],
                    updated=run["updated_at"])
    if run["status"] == "completed" and not unchanged:
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
            if green["status"] != "completed":
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
    missing = {name for key, r in state["runs"].items() if int(key) in failures
               for name in r.get("needs", [])}
    covered = {name for r in current for name in r.get("green", []) + r.get("needs", [])}
    if missing - covered and state.get("dispatched_base") != base:
        dispatch(gh, "integration", "all")
        state["dispatched_base"] = base
        return []
    if missing - covered:
        return []  # Wait for the dispatched run to appear; never treat absence as green.
    return [{"kind": "repair", "number": 0, "head": base, "base": base,
             "branch": REPAIR_BRANCH + base[:12], "runs": failures}]


def discover(gh: GitHub, app_id: int) -> dict[str, Any]:
    issue, state = monitor(gh)
    next_cursor = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    # Cursor overlaps by a minute; active runs are always polled by immutable ID.
    runs = gh.pages("actions/workflows/sql-integration.yml/runs?branch=dev&per_page=100&created=%3E%3D"
                    + quote(state.get("cursor", state["started"]), safe=""), "workflow_runs")
    for run in runs:
        _record_run(gh, state, run)
    for key, run in list(state["runs"].items()):
        if run["status"] != "completed" or run.get("needs") or run.get("pull_number") and not run.get("merged"):
            _record_run(gh, state, gh.api(f"actions/runs/{key}"))
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
        elif not pr["head"]["ref"].startswith(REPAIR_BRANCH):
            continue
        pr = gh.pull(pr["number"])
        head = pr["head"]["sha"]
        checks = gh.checks(head)
        existing = review_check(checks, base, app_id)
        denial = authorization_error(gh, pr)
        if denial:
            if existing is None or existing.get("output", {}).get("summary") != denial:
                gh.api("check-runs", "POST", {"name": REVIEW, "head_sha": head,
                    "external_id": base, "status": "completed", "conclusion": "action_required",
                    "output": {"title": "Authorization required", "summary": denial}})
            continue
        fast = fast_check(checks)
        comparison = gh.api(f"compare/{base}...{head}")
        kind = "conflict" if pr.get("mergeable") is False or comparison["behind_by"] > 0 else "review"
        if pr["head"]["ref"].startswith(REPAIR_BRANCH) and (
            fast and fast["conclusion"] in {"failure", "cancelled", "timed_out"}
            or existing and existing.get("conclusion") == "failure"
        ):
            kind = "repair"
        if fast and fast["conclusion"] in {"failure", "cancelled", "timed_out"}:
            marker = f"<!-- agent-fast-{head}-{fast['id']} -->"
            comments = gh.pages(f"issues/{pr['number']}/comments?per_page=100")
            if not any(marker in comment["body"] for comment in comments):
                gh.comment(pr["number"], marker + "\nFast CI failed; correct it before merge: "
                           + fast.get("html_url", ""))
        if (kind == "review" and existing and existing["status"] == "completed"
                and existing.get("output", {}).get("title") != "Authorization required"):
            continue
        tasks.append({"kind": kind, "number": pr["number"], "head": head,
                      "base": base, "branch": pr["head"]["ref"]})
    tasks = _integration_tasks(gh, state, base, pulls) + tasks
    save_monitor(gh, state)
    return {"include": tasks}


def prepare(gh: GitHub, task: dict[str, Any], work: Path, control: Path) -> None:
    kind = task["kind"]
    if task["number"]:
        pr = gh.pull(task["number"])
        if pr["head"]["sha"] != task["head"] or not trusted(pr, gh.repository):
            raise RuntimeError("PR changed after discovery; reconcile again.")
        denial = authorization_error(gh, pr)
        if denial:
            raise RuntimeError(denial)
    if kind in {"conflict", "repair"} and task["head"] != task["base"]:
        subprocess.run(["git", "merge", "--no-commit", "--no-ff", task["base"]], cwd=work,
                       capture_output=True, check=False)
    logs = []
    if kind == "repair":
        if not task.get("runs"):
            _, state = monitor(gh)
            task["runs"] = [int(key) for key, run in state["runs"].items()
                            if run["status"] == "completed" and run["conclusion"] != "success"]
        for run_id in task["runs"][-4:]:
            logs.append(command("gh", "run", "view", str(run_id), "--repo", gh.repository,
                                "--log-failed")[-18000:])
    if task["number"]:
        failed_checks = [c for c in gh.checks(task["head"])
                         if c["conclusion"] in {"failure", "cancelled", "timed_out"}]
        logs.append(json.dumps(failed_checks))
        fast_runs = gh.pages(f"actions/workflows/tests.yml/runs?head_sha={task['head']}&per_page=100",
                            "workflow_runs")
        for run in fast_runs[:2]:
            if run["status"] == "completed" and run["conclusion"] != "success":
                logs.append(command("gh", "run", "view", str(run["id"]), "--repo", gh.repository,
                                    "--log-failed")[-18000:])
    template = (control / f".github/agent/prompts/{kind}.md").read_text()
    context = json.dumps(task, indent=2)
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
                        task["head"], cwd=work)
        (output / "fix.patch").write_text(patch + "\n")
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
    if patch.read_text().strip():
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


def publish(gh: GitHub, task: dict[str, Any], artifact: Path, work: Path) -> None:
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
            denial = authorization_error(gh, pr)
            if denial:
                raise RuntimeError(denial)
        if task["kind"] == "review":
            verdict = json.loads((artifact / "verdict.json").read_text())
            approved = verdict.get("approved") is True
            if not isinstance(verdict.get("summary"), str) or not isinstance(verdict.get("findings"), list):
                raise RuntimeError("Malformed review verdict.")
            summary = verdict["summary"] + "\n" + "\n".join(str(x) for x in verdict["findings"])
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
            return
        patch = artifact / "fix.patch"
        if not patch.read_text().strip() and task["kind"] != "conflict":
            raise RuntimeError("No repair produced. Diagnose credentials/infrastructure explicitly.")
        if patch.read_text().strip():
            command("git", "apply", "--index", str(patch.resolve()), cwd=work)
        paths = command("git", "diff", "--cached", "--name-only", cwd=work).splitlines()
        if any(path.startswith((".git/", ".github/agent/")) or path in {".connections", ".env"}
               or path == ".github/workflows/github-agent.yml" for path in paths):
            raise RuntimeError("Repair modifies controller or sensitive files; maintainer review required.")
        if task["kind"] == "repair" and not any(path.startswith("tests/") and path.endswith(".py")
                                                 for path in paths):
            raise RuntimeError("Integration repair must include a fast regression test.")
        if visual_required(paths):
            verify_visual(artifact, patch, task)
        tree = command("git", "write-tree", cwd=work)
        parents = ["-p", task["head"]]
        if task["kind"] in {"conflict", "repair"} and task["base"] != task["head"]:
            parents.extend(["-p", task["base"]])
        commit = command("git", *signing_options(), "commit-tree", "-S", tree, *parents,
                         "-m", "GitHub agent: " + task["kind"], cwd=work)
        command("git", "push", "origin", f"{commit}:refs/heads/{task['branch']}", cwd=work)
        if not number:
            pr = gh.api("pulls", "POST", {"title": "Repair post-merge integration failures",
                "head": task["branch"], "base": "dev", "body": "Automated integration repair. "
                "Includes a fast regression test; requires independent review and fast CI."})
            gh.comment(pr["number"], "Integration runs: " + ", ".join(str(x) for x in task["runs"]))
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


def merge(gh: GitHub, app_id: int, work: Path) -> None:
    for item in gh.pulls():
        pr = gh.pull(item["number"])
        if not trusted(pr, gh.repository):
            continue
        head, base = pr["head"]["sha"], gh.base()
        if authorization_error(gh, pr):
            continue
        if not ready(pr, gh.checks(head), base, app_id):
            continue
        # Metadata folding is serialized and changes the PR head. It must pass
        # fresh CI and independent review before any merge attempt.
        command("git", "fetch", "origin", pr["head"]["ref"], cwd=work)
        command("git", "switch", "--detach", head, cwd=work)
        gh.label(pr["number"], acquire=True)
        try:
            if fold(work):
                command("git", "add", "--all", cwd=work)
                command("git", *signing_options(), "commit", "-m", "Consolidate feature changelog", cwd=work)
                command("git", "push", "origin", f"HEAD:{pr['head']['ref']}", cwd=work)
                continue
        finally:
            gh.label(pr["number"], acquire=False)
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
        gh.api(f"pulls/{pr['number']}/merge", "PUT", {"sha": head, "merge_method": "squash"})


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
