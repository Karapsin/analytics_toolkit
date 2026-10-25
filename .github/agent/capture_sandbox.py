"""Run-owned GitHub capture containers; never load candidate Python on the host."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any
import uuid

IMAGES = {"3.11": "python:3.11-bookworm", "3.13": "python:3.13-bookworm"}
DENIED = (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8",
    "169.254.0.0/16", "172.16.0.0/12", "192.0.0.0/24", "192.0.2.0/24",
    "192.168.0.0/16", "198.18.0.0/15", "198.51.100.0/24", "203.0.113.0/24",
    "224.0.0.0/4", "240.0.0.0/4",
)


def command(arguments: list[str]) -> str:
    limit = 180 if arguments[:2] == ["docker", "pull"] else 30
    result = subprocess.run(arguments, check=True, capture_output=True, text=True, timeout=limit)
    if len(result.stdout) > 1024 * 1024:
        raise ValueError("Administrative response exceeds bounds")
    return result.stdout.strip()


class Sandbox:
    """Track only resources created by this invocation; collisions never grant ownership."""

    def __init__(self, candidate: Path, trusted: Path, state_path: Path | None = None,
                 python_version: str = "3.11") -> None:
        if os.getuid() == 0 or os.getgid() == 0:
            raise ValueError("Capture requires a nonroot runner identity")
        self.candidate = candidate
        if python_version not in IMAGES:
            raise ValueError("Unreviewed container Python line")
        self.image_tag = IMAGES[python_version]
        self.trusted = trusted
        self.owner = uuid.uuid4().hex
        self.bridge = "atkv" + self.owner[:10]
        self.network = "atk-visual-" + self.owner
        self.chain = "ATKV" + self.owner[:20]
        self.network_id: str | None = None
        self.image: str | None = None
        self.chain_created = False
        self.rules: list[tuple[str, str, list[str]]] = []
        self.containers: list[str] = []
        self.configurations: dict[str, dict[str, Any]] = {}
        self.state_path = state_path
        self.operations: list[dict[str, Any]] = []

    def record(self) -> None:
        if self.state_path is None:
            return
        value = {"owner": self.owner, "bridge": self.bridge, "network_name": self.network,
                 "network_id": self.network_id, "chain": self.chain,
                 "chain_created": self.chain_created, "containers": self.containers,
                 "rules": self.rules, "operations": self.operations}
        temporary = self.state_path.with_name(self.state_path.name + "." + uuid.uuid4().hex)
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            stream.write(json.dumps(value, indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(self.state_path)
        directory = os.open(self.state_path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def mutation(self, arguments: list[str]) -> str:
        operation = {"arguments": arguments, "state": "intent"}
        self.operations.append(operation)
        self.record()  # Durable names/owner/intent precede the request to Docker/kernel.
        try:
            result = command(arguments)
            if arguments[:3] == ["docker", "network", "create"]:
                if re.fullmatch(r"[0-9a-f]{64}", result) is None:
                    raise ValueError("Network create response has no exact identity")
                self.network_id = result
            elif arguments[:2] == ["docker", "create"]:
                if re.fullmatch(r"[0-9a-f]{64}", result) is None:
                    raise ValueError("Container create response has no exact identity")
                self.containers.append(result)
            elif arguments[:2] == ["sudo", "-n"] and arguments[2] in {"iptables", "ip6tables"}:
                if arguments[5:7] == ["-N", self.chain]:
                    self.chain_created = True
                elif arguments[5] == "-I":
                    self.rules.append((arguments[2], arguments[6], arguments[8:]))
        except BaseException:
            operation["state"] = "uncertain"
            self.record()
            raise
        operation["result"] = result
        operation["state"] = "confirmed"
        self.record()
        return result

    def uncertain_operations(self) -> list[dict[str, Any]]:
        return [item for item in self.operations if item["state"] != "confirmed"]

    def change_firewall(self, binary: str, *arguments: str) -> str:
        return self.mutation(["sudo", "-n", binary, "-w", "5", *arguments])

    def firewall(self, binary: str, *arguments: str) -> str:
        return command(["sudo", "-n", binary, "-w", "5", *arguments])

    def _insert(self, binary: str, chain: str, rule: list[str]) -> None:
        self.change_firewall(binary, "-I", chain, "1", *rule)
        self.record()

    def _chain_rules(self) -> list[str]:
        return ["-N " + self.chain, *[
            "-A " + self.chain + " -d " + network + " -j REJECT --reject-with icmp-port-unreachable"
            for network in DENIED
        ], "-A " + self.chain + " -j RETURN"]

    def setup(self) -> None:
        if self.state_path is None:
            raise ValueError("Durable run-owned resource journal required")
        self.record()
        command(["docker", "pull", self.image_tag])
        self.image = command(["docker", "image", "inspect", "--format", "{{.Id}}", self.image_tag])
        if re.fullmatch(r"sha256:[0-9a-f]{64}", self.image) is None:
            raise ValueError("Immutable container image identity missing")
        self.network_id = self.mutation([
            "docker", "network", "create", "--driver", "bridge", "--label",
            "atk.visual.owner=" + self.owner, "--opt",
            "com.docker.network.bridge.name=" + self.bridge, self.network,
        ])
        if re.fullmatch(r"[0-9a-f]{64}", self.network_id) is None:
            raise ValueError("Owned network identity missing")
        self.record()
        self.change_firewall("iptables", "-N", self.chain)
        self.chain_created = True
        for network in DENIED:
            self.change_firewall("iptables", "-A", self.chain, "-d", network, "-j", "REJECT")
        self.change_firewall("iptables", "-A", self.chain, "-j", "RETURN")
        # A direct first FORWARD guard also fails closed across backend routing changes.
        self._insert("iptables", "DOCKER-USER", ["-i", self.bridge, "-j", self.chain])
        self._insert("iptables", "FORWARD", ["-i", self.bridge, "-j", self.chain])
        self._insert("iptables", "INPUT", ["-i", self.bridge, "-j", "REJECT"])
        for chain in ("INPUT", "FORWARD"):
            self._insert("ip6tables", chain, ["-i", self.bridge, "-j", "REJECT"])
        self.verify()

    def verify(self) -> None:
        if self.network_id is None or not self.chain_created:
            raise ValueError("Network guards not installed")
        network = json.loads(command(["docker", "network", "inspect", self.network_id]))
        if len(network) != 1 or not all((
            network[0]["Id"] == self.network_id,
            network[0]["Name"] == self.network,
            network[0]["Driver"] == "bridge",
            network[0]["EnableIPv6"] is False,
            network[0]["Labels"].get("atk.visual.owner") == self.owner,
            network[0]["Options"].get("com.docker.network.bridge.name") == self.bridge,
        )):
            raise ValueError("Owned network changed")
        if self.firewall("iptables", "-S", self.chain).splitlines() != self._chain_rules():
            raise ValueError("Owned egress policy changed")
        for binary, chain, rule in self.rules:
            self.firewall(binary, "-C", chain, *rule)
            first = next((line for line in self.firewall(binary, "-S", chain).splitlines()
                          if line.startswith("-A ")), "")
            expected = "-A " + chain + " " + " ".join(rule)
            if rule[-1] == "REJECT":
                expected += " --reject-with " + (
                    "icmp6-port-unreachable" if binary == "ip6tables" else "icmp-port-unreachable"
                )
            if first != expected:
                raise ValueError("Owned guard is not first in its chain")

    def create(self, phase: str, arguments: list[str], identity: dict[str, Any]) -> str:
        self.verify()
        if self.image is None or phase not in {"install", "capture", "probe"}:
            raise ValueError("Unknown contained phase")
        environment = {"HOME": "/tmp", "TMPDIR": "/tmp", "PYTHONPATH": "/work",
                       "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
                       "CANDIDATE": identity["candidate"], "GITHUB_REPOSITORY": identity["repository"],
                       "GITHUB_RUN_ID": str(identity["run_id"]),
                       "GITHUB_RUN_ATTEMPT": str(identity["run_attempt"])}
        options = ["docker", "create", "--name", "atk-visual-" + self.owner + "-" + phase,
                   "--label", "atk.visual.owner=" + self.owner, "--read-only", "--cap-drop", "ALL",
                   "--security-opt", "no-new-privileges=true", "--pids-limit", "512",
                   "--memory", "6g", "--cpus", "2", "--user", f"{os.getuid()}:{os.getgid()}",
                   "--network", "none" if phase == "capture" else self.network_id,
                   "--tmpfs", "/tmp:rw,exec,nosuid,nodev,size=3g,mode=1777", "--workdir", "/work",
                   "--mount", f"type=bind,source={self.candidate},target=/work",
                   "--mount", f"type=bind,source={self.trusted},target=/trusted,readonly",
                   "--log-driver", "none"]
        for name, value in environment.items():
            options.extend(["--env", name + "=" + value])
        container = self.mutation([*options, self.image, *arguments])
        if re.fullmatch(r"[0-9a-f]{64}", container) is None:
            raise ValueError("Owned container identity missing")
        self.record()
        defaults = json.loads(command(["docker", "image", "inspect", "--format",
                                       "{{json .Config.Env}}", self.image]))
        expected_environment = dict(item.split("=", 1) for item in defaults)
        expected_environment.update(environment)
        self.configurations[container] = {"environment": expected_environment,
            "network": "none" if phase == "capture" else self.network_id,
            "arguments": arguments}
        self.validate_container(container)
        return container

    def _container(self, container: str) -> None:
        info = json.loads(command(["docker", "inspect", container]))
        if len(info) != 1 or info[0]["Id"] != container or (
            info[0]["Config"]["Labels"].get("atk.visual.owner") != self.owner
        ):
            raise ValueError("Container ownership changed")

    def validate_container(self, container: str) -> None:
        self._container(container)
        info = json.loads(command(["docker", "inspect", container]))[0]
        config, host = info["Config"], info["HostConfig"]
        expected = self.configurations[container]
        environment = dict(item.split("=", 1) for item in config["Env"])
        mounts = {item["Destination"]: item for item in info["Mounts"]}
        if not all((
            info["Image"] == self.image, config["Image"] == self.image,
            config["User"] == f"{os.getuid()}:{os.getgid()}",
            config["WorkingDir"] == "/work", not config.get("Entrypoint"),
            config["Cmd"] == expected["arguments"], environment == expected["environment"],
            len(config["Env"]) == len(environment), host["ReadonlyRootfs"] is True,
            host["Privileged"] is False, not host.get("CapAdd"), host["CapDrop"] == ["ALL"],
            any(x in {"no-new-privileges", "no-new-privileges=true"} for x in host["SecurityOpt"]),
            host["NetworkMode"] == expected["network"], not host.get("PortBindings"),
            host["PublishAllPorts"] is False, not host.get("Devices"), not host.get("Binds"),
            host["PidMode"] != "host", host["IpcMode"] == "private", host["UTSMode"] != "host",
            host["PidsLimit"] == 512, host["Memory"] == 6 * 1024**3,
            host["NanoCpus"] == 2 * 10**9, set(host["Tmpfs"]) == {"/tmp"},
            set(host["Tmpfs"]["/tmp"].split(",")) == {
                "rw", "exec", "nosuid", "nodev", "size=3g", "mode=1777"},
            set(mounts) == {"/work", "/trusted"},
        )):
            raise ValueError("Actual container isolation differs from the reviewed configuration")
        for target, source, writable in (("/work", self.candidate, True),
                                          ("/trusted", self.trusted, False)):
            mount = mounts[target]
            if mount["Type"] != "bind" or mount["Source"] != str(source) or mount["RW"] is not writable:
                raise ValueError("Actual candidate/trusted mount isolation differs")

    def remove(self, container: str) -> None:
        if container not in self.containers:
            raise ValueError("Unowned container")
        self._container(container)
        self.mutation(["docker", "rm", "--force", container])
        # Removal success is checked independently; never collect while a child survives.
        remaining = command(["docker", "ps", "--all", "--quiet", "--no-trunc",
                             "--filter", "id=" + container])
        if remaining:
            raise ValueError("Owned container remains")
        self.containers.remove(container)
        self.record()

    def cleanup(self) -> None:
        for container in list(self.containers):
            self.remove(container)
        if self.containers:
            raise ValueError("Cannot release guards while candidates remain")
        if self.uncertain_operations():
            raise ValueError("Resource mutation response uncertain; preserve guards and journal")
        # Read back the complete policy before deleting any recorded authority.
        if self.chain_created and self.rules:
            self.verify()
        for binary, chain, rule in reversed(self.rules):
            self.firewall(binary, "-C", chain, *rule)
            self.change_firewall(binary, "-D", chain, *rule)
        self.rules.clear()
        if self.chain_created:
            self.change_firewall("iptables", "-F", self.chain)
            self.change_firewall("iptables", "-X", self.chain)
            self.chain_created = False
        if self.network_id:
            self.mutation(["docker", "network", "rm", self.network_id])
            self.network_id = None
        self.record()
