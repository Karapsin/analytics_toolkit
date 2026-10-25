"""Exact reviewed45 components with only GitHub/Docker/OS subprocess fakes.

No native platform or actual sentinel execution is claimed by these fixtures.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
from pathlib import Path

from tests._support.paths import REPO_ROOT
from tests.repository._support.capture_profile import FIXTURE, D, R, envelope, environment, event


def git(root, *args, raw_stdout=False):
    raw = subprocess.run(["git", *args], cwd=root, capture_output=True, check=True).stdout
    return raw if raw_stdout else raw.decode().strip()


def source_checkout(tmp_path):
    root = tmp_path / "control"
    root.mkdir()
    sources = json.loads(FIXTURE.read_text())["sources"]
    for relative in R.SOURCE_PATHS:
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        key = destination.stem
        if key in sources:
            raw = sources[key]["source"].encode()
            assert D.digest(raw) == sources[key]["sha256"]
        else:
            raw = (REPO_ROOT / relative).read_bytes()
        destination.write_bytes(raw)
        destination.chmod(0o644)
    git(root, "init", "--initial-branch=fixture")
    git(root, "config", "user.name", "Synthetic source fixture")
    git(root, "config", "user.email", "fixture@example.test")
    git(root, "add", "--all")
    git(
        root,
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-m",
        "Synthetic component source; no authority",
    )
    head = git(root, "rev-parse", "HEAD")
    path = root / ".github/agent/capture_receiver.py"
    spec = importlib.util.spec_from_file_location("isolated_component_receiver", path)
    assert spec is not None
    assert spec.loader is not None
    receiver = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(receiver)
    value = envelope()
    value["source"]["expected_trusted_head"] = head
    value["source"]["source_tag"] = "agent-visual-source-v1-" + head
    value["source"]["source_set_sha256"] = D.digest(
        D.canonical(receiver.source_inventory(root, head))
    )
    value["authorization"]["fixture_source_commit"] = head
    value["authorization"]["fixture_manifest_sha256"] = D.digest(
        (root / (R.FIXTURE + "manifest.json")).read_bytes()
    )
    value["authorization"]["fixture_program_sha256"] = D.digest(
        (root / (R.FIXTURE + "sentinel.py")).read_bytes()
    )
    env = environment(value)
    env.update(
        GITHUB_SHA=head,
        GITHUB_REF="refs/tags/agent-visual-source-v1-" + head,
        INPUT_EXPECTED_TRUSTED_HEAD=head,
        INPUT_ENVELOPE=D.canonical(value).decode(),
    )
    synthetic = event(value)
    run = {
        "id": 99,
        "run_attempt": 1,
        "path": R.T.WORKFLOW,
        "event": "workflow_dispatch",
        "head_sha": head,
        "head_branch": value["source"]["source_tag"],
        "repository": {"full_name": R.T.REPOSITORY},
        "workflow_id": 88,
    }

    class API:
        def __init__(self):
            self.moves = False

        def request(self, path):
            if path == "git/ref/heads/main":
                result = {
                    "ref": "refs/heads/main",
                    "object": {"type": "commit", "sha": "f" * 40 if self.moves else head},
                }
            elif path.startswith("git/ref/tags/"):
                result = {
                    "ref": "refs/tags/" + value["source"]["source_tag"],
                    "object": {"type": "commit", "sha": head},
                }
            elif path.startswith("issues?state=open&labels=agent:state&per_page=100&page="):
                result = [synthetic["issue"]]
            else:
                result = {
                    "issues/comments/77": synthetic["comment"],
                    "issues/12": synthetic["issue"],
                    "actions/runs/99": run,
                }[path]
            raw = D.canonical(result)
            return 200, raw, {"body_complete": True}

    return receiver, value, env, API()


class NativeAdmin:
    def __init__(self, receiver, report, root, *, fault=None):
        self.receiver = receiver
        self.report = report
        self.root = root
        self.timeout = fault == "timeout"
        self.cleanup_error = fault == "cleanup"
        self.fault = fault
        self.starts = 0
        self.commands = []
        self.sandbox = None

    def command(self, args):
        self.commands.append(args)
        s = self.sandbox
        creates = {("docker", "network", "create"): "1" * 64, ("docker", "create"): "2" * 64}
        key = tuple(args[:3]) if args[:3] == ["docker", "network", "create"] else tuple(args[:2])
        if key in creates:
            return creates[key]
        if args[:3] == ["docker", "network", "inspect"]:
            return json.dumps(
                [
                    {
                        "Id": s.network_id,
                        "Name": s.network,
                        "Driver": "bridge",
                        "EnableIPv6": False,
                        "Labels": {"atk.visual.owner": s.owner},
                        "Options": {"com.docker.network.bridge.name": s.bridge},
                    }
                ]
            )
        if args[:3] == ["docker", "image", "inspect"]:
            return (
                json.dumps(["PATH=/usr/local/bin"])
                if args[4] == "{{json .Config.Env}}"
                else "sha256:" + "7" * 64
                if self.fault == "image"
                else self.receiver.NATIVE_IMAGE_311
            )
        if args[:2] == ["docker", "inspect"]:
            expected = s.configurations[args[-1]]
            return json.dumps(
                [
                    {
                        "Id": args[-1],
                        "Image": s.image,
                        "Config": {
                            "Labels": {"atk.visual.owner": s.owner},
                            "Image": s.image,
                            "User": f"{os.getuid()}:{os.getgid()}",
                            "WorkingDir": "/work",
                            "Entrypoint": None,
                            "Cmd": expected["arguments"],
                            "Env": [k + "=" + v for k, v in expected["environment"].items()],
                        },
                        "HostConfig": {
                            "ReadonlyRootfs": True,
                            "Privileged": False,
                            "CapDrop": ["ALL"],
                            "SecurityOpt": ["no-new-privileges=true"],
                            "NetworkMode": expected["network"],
                            "PublishAllPorts": False,
                            "PidMode": "",
                            "IpcMode": "private",
                            "UTSMode": "",
                            "PidsLimit": 512,
                            "Memory": 6 * 1024**3,
                            "NanoCpus": 2 * 10**9,
                            "Tmpfs": {"/tmp": "rw,exec,nosuid,nodev,size=3g,mode=1777"},
                        },
                        "Mounts": [
                            {
                                "Destination": dest,
                                "Type": "bind",
                                "Source": str(source),
                                "RW": writable,
                            }
                            for dest, source, writable in (
                                ("/work", s.candidate, True),
                                ("/trusted", s.trusted, False),
                            )
                        ],
                    }
                ]
            )
        if args[:2] == ["docker", "rm"] and self.cleanup_error:
            msg = "Synthetic administrative removal failure"
            raise RuntimeError(msg)
        if args[:2] == ["sudo", "-n"] and args[5] == "-S":
            chain = args[6]
            return self.firewall_readback(args, chain)
        return ""

    def firewall_readback(self, args, chain):
        s = self.sandbox
        if chain == s.chain:
            return "\n".join(s._chain_rules())
        rules = [
            rule for binary, item, rule in reversed(s.rules) if binary == args[2] and item == chain
        ]
        return "\n".join(
            "-A "
            + chain
            + " "
            + " ".join(rule)
            + (
                " --reject-with "
                + ("icmp6-port-unreachable" if args[2] == "ip6tables" else "icmp-port-unreachable")
                if rule[-1] == "REJECT"
                else ""
            )
            for rule in rules
        )

    def popen(self, args, **_kwargs):
        assert args == ["docker", "start", "--attach", "2" * 64]
        self.starts += 1
        settings = {
            "nonce": self.report["envelope"]["attempt"]["nonce"],
            "trusted_head": self.report["envelope"]["source"]["expected_trusted_head"],
            "purpose": "receiver-rehearsal",
        }
        marker = dict(
            settings,
            marker="receiver-rehearsal-v1",
            visual_approval=False,
            prerequisite_satisfied=False,
        )
        with self.receiver.D.Store(self.sandbox.candidate) as store:
            store.put(("sentinel-result.json",), D.canonical(marker))
        timeout = self.timeout

        class Process:
            stdout = io.BytesIO(D.canonical(marker))
            stderr = io.BytesIO(b"")
            returncode = 0

            def wait(self, **_kwargs):
                if timeout:
                    raise subprocess.TimeoutExpired(args, 30)
                return 0

            def kill(self):
                self.returncode = -9

        return Process()


def rehearsal_runtime(tmp_path, monkeypatch, fault):
    receiver, _value, env, api = source_checkout(tmp_path)
    report = receiver.preflight(env, api, now=100)
    runner = tmp_path / "runner"
    runner.mkdir()
    root = runner / ("capture-profile-" + "1" * 32)
    root.mkdir(mode=0o700)
    with receiver.D.Store(root) as store:
        store.put(("preflight.json",), receiver.D.canonical(report))
        store.put(
            ("budget.json",),
            receiver.D.canonical({"limit": 16 * 1024 * 1024, "used": api.budget.used + 1024}),
        )
    monkeypatch.setenv("RUNNER_TEMP", str(runner))
    monkeypatch.setattr(receiver.time, "time", lambda: 100)
    sandbox_module = receiver.D.sibling("capture_sandbox")
    isolation_module = receiver.D.sibling("capture_isolation")
    admin = NativeAdmin(
        receiver,
        report,
        root,
        fault=fault,
    )
    original_init = sandbox_module.Sandbox.__init__

    def init(instance, *args, **kwargs):
        original_init(instance, *args, **kwargs)
        admin.sandbox = instance

    monkeypatch.setattr(sandbox_module.Sandbox, "__init__", init)
    monkeypatch.setattr(sandbox_module, "command", admin.command)
    original_popen = isolation_module.subprocess.Popen

    def popen(args, **kwargs):
        return (
            admin.popen(args, **kwargs)
            if args[:2] == ["docker", "start"]
            else original_popen(args, **kwargs)
        )

    monkeypatch.setattr(isolation_module.subprocess, "Popen", popen)
    if fault == "unsafe-logs":
        break_quarantine(isolation_module, monkeypatch)
    return receiver, report, runner, root, admin, isolation_module


def break_quarantine(isolation_module, monkeypatch):
    # Exercise the actual sealed contained_output lifecycle: uncertain live
    # reader and failed quarantine rename. No writer starts in this fixture.
    class Reader:
        def __init__(self, **_kwargs):
            pass

        def start(self):
            pass

        def join(self, **_kwargs):
            pass

        def is_alive(self):
            return True

    original_rename = Path.rename

    def rename(path, target):
        if path.name == "streams":
            message = "Synthetic quarantine refusal"
            raise OSError(message)
        return original_rename(path, target)

    monkeypatch.setattr(isolation_module.threading, "Thread", Reader)
    monkeypatch.setattr(Path, "rename", rename)
