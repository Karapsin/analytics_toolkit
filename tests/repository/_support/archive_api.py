from __future__ import annotations

import base64
import importlib.util
import os
import subprocess

from tests._support.paths import REPO_ROOT

_RUN = subprocess.run


def protocol():
    spec = importlib.util.spec_from_file_location(
        "archive_test_protocol", REPO_ROOT / ".github/agent/task_context.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def large_context(proto, *, version=2):
    plan = "Preserve requested editor semantics and all independent work.\n" * 90
    value = {
        "version": 1,
        "task_id": "large-task",
        "producer_id": "local:large-task",
        "source": "user",
        "starting_prompt": "Fix the requested editor behavior.",
        "corrections": ["Implement the plan."],
    }
    for sequence in range(21):
        value["corrections"].append(
            "User correction "
            + str(sequence)
            + ":\n"
            + (
                "Retain <all> questions & answers, Unicode "
                "\u03b1\U0001f4dd and literal <!-- markers.\n" * 35
            )
        )
    if version == 2:
        value.update(
            version=2,
            plans=[{"id": proto.plan_id(plan), "text": plan, "approval_index": 0}],
            images=[],
            replacements=[],
            decisions=[{"questions": ["Which implementation?"], "choice": "Recommended"}],
            questions=[
                {
                    "id": proto.question_id("Question " + str(i), i),
                    "text": "Question " + str(i),
                    "answer_index": i + 1,
                }
                for i in range(5)
            ],
        )
    return value


class ArchiveAPI:
    """GitHub metadata fake backed by real Git object serialization, offline."""

    def __init__(self, root):
        self.directory = root / "objects.git"
        self.environment = dict(
            os.environ,
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_CONFIG_SYSTEM=os.devnull,
            GIT_CONFIG_COUNT="0",
        )
        _RUN(
            ["git", "init", "--bare", str(self.directory)],
            check=True,
            capture_output=True,
            timeout=10,
            env=self.environment,
        )
        self.refs = {}
        self.calls = []
        self.mutations = 0
        self.fail_after = None
        self.corrupt = {}

    def git(self, *args, payload=None, environment=None):
        result = _RUN(
            ["git", "--git-dir", str(self.directory), *args],
            input=payload,
            capture_output=True,
            check=True,
            timeout=10,
            env=environment or self.environment,
        )
        return result.stdout

    def __call__(self, path, method="GET", data=None):
        self.calls.append((path, method))
        if method == "GET":
            return self._get(path)
        assert method == "POST"
        handlers = {
            "git/blobs": self._blob,
            "git/trees": self._tree,
            "git/commits": self._commit,
            "git/refs": self._ref,
        }
        if path not in handlers:
            message = "Unexpected GitHub mutation: " + path
            raise AssertionError(message)
        result = handlers[path](data)
        self.mutations += 1
        if self.mutations == self.fail_after:
            message = "Simulated lost response after accepted metadata write"
            raise RuntimeError(message)
        return result

    def _get(self, path):
        if path.startswith("git/ref/"):
            ref = "refs/" + path[len("git/ref/") :]
            return {"ref": ref, "object": {"type": "commit", "sha": self.refs.get(ref)}}
        if path.startswith("git/commits/"):
            sha = path.rsplit("/", 1)[1]
            raw = self.git("cat-file", "commit", sha).decode()
            headers, message = raw.split("\n\n", 1)
            tree = headers.splitlines()[0].split()[1]
            # GitHub's GET response omits the raw commit's terminal LF.
            return {
                "sha": sha,
                "tree": {"sha": tree},
                "message": message.rstrip("\n"),
                "parents": [],
            }
        if path.startswith("git/trees/"):
            sha = path.rsplit("/", 1)[1]
            rows = self.git("ls-tree", "-z", sha).decode().strip("\0").split("\0")
            entries = []
            for row in rows:
                metadata, name = row.split("\t", 1)
                mode, kind, blob = metadata.split()
                entries.append({"path": name, "mode": mode, "type": kind, "sha": blob})
            return {"sha": sha, "tree": entries, "truncated": False}
        if path.startswith("git/matching-refs/"):
            prefix = "refs/" + path[len("git/matching-refs/") :]
            return [
                {
                    "ref": ref,
                    "object": {
                        "type": "commit",
                        "sha": sha,
                        "url": "https://api.github.invalid/object",
                    },
                }
                for ref, sha in self.refs.items()
                if ref.startswith(prefix)
            ]
        if path.startswith("git/blobs/"):
            sha = path.rsplit("/", 1)[1]
            payload = self.corrupt.get(sha)
            if payload is None:
                payload = self.git("cat-file", "blob", sha)
            return {
                "sha": sha,
                "size": len(payload),
                "encoding": "base64",
                "content": base64.encodebytes(payload).decode(),
            }
        message = "Unexpected GitHub lookup: " + path
        raise AssertionError(message)

    def _blob(self, data):
        assert data["encoding"] == "base64"
        sha = (
            self.git(
                "hash-object",
                "-w",
                "--stdin",
                payload=base64.b64decode(data["content"], validate=True),
            )
            .decode()
            .strip()
        )
        return {"sha": sha}

    def _tree(self, data):
        entries = "".join(
            f"{e['mode']} {e['type']} {e['sha']}\t{e['path']}\n" for e in data["tree"]
        )
        return {"sha": self.git("mktree", payload=entries.encode()).decode().strip()}

    def _commit(self, data):
        assert data["parents"] == []
        environment = dict(self.environment)
        for role in ["author", "committer"]:
            for field in ["name", "email", "date"]:
                environment["GIT_" + role.upper() + "_" + field.upper()] = data[role][field]
        sha = (
            self.git(
                "commit-tree",
                data["tree"],
                payload=data["message"].encode(),
                environment=environment,
            )
            .decode()
            .strip()
        )
        return {"sha": sha}

    def _ref(self, data):
        assert data["ref"].startswith("refs/tags/agent-task-evidence/")
        if data["ref"] in self.refs:
            message = "Reference already exists"
            raise RuntimeError(message)
        self.git("update-ref", data["ref"], data["sha"])
        self.refs[data["ref"]] = data["sha"]
        return {"ref": data["ref"], "object": {"sha": data["sha"], "type": "commit"}}
