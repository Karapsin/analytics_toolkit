from __future__ import annotations

import json
import subprocess
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path


class FaultController:
    def __init__(self, *, root: Path, project: str, artifact_dir: Path) -> None:
        self.root = root
        self.project = project
        self.artifact_dir = artifact_dir
        self.timeline: list[dict[str, Any]] = []
        self.restore_actions: list[tuple[str, str]] = []

    def pause(self, service: str) -> None:
        self._act("pause", service)
        self.restore_actions.append(("unpause", service))

    def unpause(self, service: str) -> None:
        self._act("unpause", service)
        self.restore_actions = [
            item for item in self.restore_actions if item != ("unpause", service)
        ]

    def restart(self, service: str) -> None:
        self._act("restart", service)

    def stop(self, service: str) -> None:
        self._act("stop", service)
        self.restore_actions.append(("restart", service))

    def wait_healthy(self, service: str, *, timeout: float = 120) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = self._run("ps", "--format", "json", service, check=False)
            if result.returncode == 0 and '"Health":"healthy"' in result.stdout.replace(" ", ""):
                self._record("healthy", service, True)
                return
            time.sleep(1)
        self._record("healthy", service, False)
        msg = f"service did not become healthy: {service}"
        raise TimeoutError(msg)

    def restore(self) -> None:
        errors: list[str] = []
        for action, service in reversed(self.restore_actions):
            try:
                self._act(action, service)
            except RuntimeError as exc:  # noqa: PERF203 - every restore must be attempted.
                errors.append(str(exc))
        self.restore_actions.clear()
        self.write_timeline()
        if errors:
            raise RuntimeError("; ".join(errors))

    def write_timeline(self) -> None:
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        (self.artifact_dir / "fault-timeline.json").write_text(
            json.dumps(self.timeline, indent=2), encoding="utf-8"
        )

    def _act(self, action: str, service: str) -> None:
        services = self._run("config", "--services").stdout.split()
        if service not in services:
            msg = f"service is outside the integration Compose project: {service}"
            raise ValueError(msg)
        result = self._run(action, service, check=False)
        self._record(action, service, result.returncode == 0)
        self.write_timeline()
        if result.returncode != 0:
            raise RuntimeError(result.stderr or result.stdout)

    def _record(self, action: str, service: str, ok: bool) -> None:
        self.timeline.append(
            {
                "timestamp_monotonic": time.monotonic(),
                "action": action,
                "service": service,
                "ok": ok,
            }
        )

    def _run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        command = [
            "docker",
            "compose",
            "--project-name",
            self.project,
            "--file",
            str(self.root / "integration/docker-compose.yml"),
            "--file",
            str(self.root / "integration/docker-compose.auth.yml"),
            "--profile",
            "auth",
            *args,
        ]
        return subprocess.run(
            command,
            cwd=self.root,
            check=check,
            capture_output=True,
            text=True,
        )
