"""Start native Plan mode in Codex or Claude Code before forwarding user input."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import select
import signal
import subprocess
import sys
import time
from pathlib import Path

from agent_tools import agent_runtimes, bootstrap_process, machine_signing, sessions

SESSION_ID_LENGTH = 16


class Bootstrap:
    """Observe the TUI, selecting /plan only after its command menu appears."""

    def __init__(self) -> None:
        self.stage = "loading"
        self.buffer = ""
        self.intro_ready = False
        self.trust_accepted = False

    def feed(self, data: bytes) -> bytes:
        self.buffer = (self.buffer + data.decode("utf-8", errors="replace"))[-32000:]
        text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", self.buffer)
        if (
            self.stage == "loading"
            and not self.trust_accepted
            and "Folder access" in text
            and "Trust and continue" in text
        ):
            # The launcher created and synchronized this private clone itself.
            self.trust_accepted, self.buffer = True, ""
            return b"\x1b[13u"
        self.intro_ready = self.intro_ready or "Tip:" in text
        if self.stage == "loading" and self.intro_ready and re.search(r"GPT-|context left", text):
            self.stage, self.buffer = "menu", ""
            return b"/plan"
        if self.stage == "menu" and "switch to Plan mode" in text:
            self.stage, self.buffer = "verify", ""
            return b"\r"
        if self.stage == "verify" and re.search(r"GPT-[^\n]*Plan mode", text):
            self.stage = "ready"
        return b""


def _forward_input(master: int, stdin: int, bootstrap: Bootstrap) -> bool:
    data = os.read(stdin, 65536)
    if not data:
        return False
    if bootstrap.stage == "ready" or data.startswith(b"\x1b"):
        os.write(master, data)
    elif b"\x03" in data:
        raise KeyboardInterrupt
    return True


def _pump(master: int, stdin: int, deadline: float) -> None:
    bootstrap = Bootstrap()
    next_trust_confirmation = time.monotonic() + 0.5
    while True:
        if bootstrap.stage != "ready" and time.monotonic() > deadline:
            msg = "Could not verify native Plan mode; no task input was forwarded."
            message = msg
            raise RuntimeError(message)
        readable, _, _ = select.select([master, stdin], [], [], 0.2)
        if master in readable:
            try:
                output = os.read(master, 65536)
            except OSError:
                return
            if not output:
                return
            os.write(sys.stdout.fileno(), output)
            command = bootstrap.feed(output)
            if command:
                os.write(master, command)
        if stdin in readable and not _forward_input(master, stdin, bootstrap):
            return
        if (
            bootstrap.stage == "loading"
            and bootstrap.trust_accepted
            and not bootstrap.intro_ready
            and time.monotonic() >= next_trust_confirmation
        ):
            # Onboarding discards keys queued during its first render. Retry only
            # the launcher-owned trust confirmation; task input remains held.
            os.write(master, b"\x1b[13u")
            next_trust_confirmation = time.monotonic() + 0.5


def launch(executable: str, root: Path, *, timeout: float = 90) -> int:
    # Keep pure bootstrap tests importable on Windows; this terminal adapter is Unix-only.
    import fcntl  # noqa: PLC0415
    import pty  # noqa: PLC0415
    import termios  # noqa: PLC0415
    import tty  # noqa: PLC0415

    if not sys.stdin.isatty() or not sys.stdout.isatty():
        msg = "Interactive launcher requires a terminal; use codex exec separately."
        message = msg
        raise RuntimeError(message)
    stdin = sys.stdin.fileno()
    previous = termios.tcgetattr(stdin)
    pid, master = pty.fork()
    if pid == 0:
        os.chdir(root)
        os.execv(  # noqa: S606 - explicit installed CLI executable, without a shell.
            executable,
            agent_runtimes.RUNTIMES["codex"].interactive_argv(executable, root),
        )
    status = 0
    deadline = time.monotonic() + timeout

    def resize(_signum: int = 0, _frame: object = None) -> None:
        window = fcntl.ioctl(stdin, termios.TIOCGWINSZ, b"\0" * 8)
        fcntl.ioctl(master, termios.TIOCSWINSZ, window)
        os.kill(pid, signal.SIGWINCH)

    previous_resize = signal.signal(signal.SIGWINCH, resize)
    try:
        resize()
        tty.setraw(stdin)
        _pump(master, stdin, deadline)
        _, status = os.waitpid(pid, 0)
        return os.waitstatus_to_exitcode(status)
    finally:
        termios.tcsetattr(stdin, termios.TCSADRAIN, previous)
        signal.signal(signal.SIGWINCH, previous_resize)
        os.close(master)
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGTERM)
        with contextlib.suppress(ChildProcessError):
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if os.waitpid(pid, os.WNOHANG)[0]:
                    break
                time.sleep(0.05)
            else:
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)


def launch_claude(executable: str, root: Path) -> int:
    """Replace the launcher with Claude Code, natively in Plan mode from its first prompt."""
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        msg = "Interactive launcher requires a terminal; use claude -p separately."
        raise RuntimeError(msg)
    config = root / agent_runtimes.CLAUDE_LAUNCH_CONFIG
    config.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    config.write_text(agent_runtimes.render_mcp_json(root))
    environment = os.environ.copy()
    environment.update(agent_runtimes.claude_launch_env())
    os.chdir(root)
    os.execve(  # noqa: S606 - explicit installed CLI executable, without a shell.
        executable,
        agent_runtimes.RUNTIMES["claude"].interactive_argv(executable, root),
        environment,
    )
    return 0  # pragma: no cover - execve does not return


def _resume_root(source: Path, directory: Path, identity: str) -> Path:
    if len(identity) != SESSION_ID_LENGTH or any(
        char not in "0123456789abcdef" for char in identity
    ):
        message = "Invalid bootstrap session ID."
        raise RuntimeError(message)
    root = directory / identity
    if root.resolve() != directory.resolve() / identity or not (root / ".git").is_dir():
        message = "Bootstrap session must be an owned clone in the session directory."
        raise RuntimeError(message)
    try:
        state = sessions.load(root)
    except (ValueError, OSError) as exc:
        message = "Bootstrap session receipt is invalid; preserve the clone."
        raise RuntimeError(message) from exc
    if (
        not isinstance(state, dict)
        or state.get("id") != identity
        or state.get("source") != str(source)
        or state.get("phase") != "launching"
    ):
        message = "Resume requires this repository's session in launching phase."
        raise RuntimeError(message)
    if sessions.run(root, "git", "rev-parse", "--show-toplevel") != str(root.resolve()):
        message = "Bootstrap session has an unexpected Git root."
        raise RuntimeError(message)
    sessions.clean(root)
    if sessions.run(root, "git", "branch", "--show-current") != "dev":
        message = "Bootstrap resume requires dev; preserve the existing branch."
        raise RuntimeError(message)
    return root


def _prepare(source: Path, root: Path, *, refresh: bool, runtime: str = "codex") -> None:
    directory = root / ".rag_index/bootstrap"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    log = directory / "launcher.log"
    environment = os.environ.copy()
    environment.update(
        {
            "MCP_PYTHON": sys.executable,
            "ATK_BOOTSTRAP_PROGRESS": "1",
            "ATK_BOOTSTRAP_CACHE_DIR": str(bootstrap_env_directory(source)),
            "ATK_BOOTSTRAP_REFRESH": "1" if refresh else "0",
        }
    )
    result = subprocess.run(
        [
            str(root / "agent_tools/mcp_tool.sh"),
            "prepare-start",
            "--task",
            f"New isolated {agent_runtimes.RUNTIMES[runtime].display} session",
            "--root",
            str(root),
        ],
        cwd=root,
        env=environment,
        stdout=subprocess.PIPE,
        text=True,
        check=False,
    )
    descriptor = os.open(log, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w") as output:
        output.write(result.stdout)
    receipt: dict = {}
    try:
        receipt = json.loads(result.stdout)
        ready = isinstance(receipt, dict) and receipt.get("ok") is True
    except ValueError:
        ready = False
    if result.returncode != 0 or not ready:
        message = f"Session preparation failed ({_failure_summary(receipt)}); receipt: {log}"
        raise RuntimeError(message)


def _failure_summary(receipt: object) -> str:
    if not isinstance(receipt, dict):
        return "invalid preparation receipt"
    blockers = receipt.get("blockers")
    blocker = blockers[0] if isinstance(blockers, list) and blockers else {}
    blocker = blocker if isinstance(blocker, dict) else {}
    details = receipt.get("result")
    phase = details.get("phase", "bootstrap") if isinstance(details, dict) else "bootstrap"
    excerpt = str(blocker.get("message") or blocker.get("excerpt") or receipt.get("error", ""))
    if any(error in excerpt.lower() for error in bootstrap_process.NETWORK_ERRORS):
        return f"{phase}: transient network failure"
    lines = [line.strip() for line in excerpt.splitlines() if line.strip()]
    cause = next(
        (line for line in lines if line.startswith("ERROR:")), lines[-1] if lines else "failed"
    )
    return f"{phase}: {cause[:200]}"


def bootstrap_env_directory(source: Path) -> Path:
    override = os.environ.get("ATK_BOOTSTRAP_CACHE_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return (
        Path.home()
        / ".local/share/analytics-toolkit-agent/wheels"
        / sessions.repository_key(source)
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", choices=sorted(agent_runtimes.RUNTIMES), default="codex")
    parser.add_argument("source", type=Path)
    parser.add_argument("executable")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--resume-bootstrap")
    mode.add_argument("--refresh-bootstrap-cache", action="store_true")
    args = parser.parse_args(argv)
    source = args.source.resolve()
    reviewer = os.environ.get(agent_runtimes.REVIEWER_ENV) or args.runtime
    if reviewer not in agent_runtimes.RUNTIMES:
        message = f"Unknown local reviewer runtime: {reviewer}"
        raise RuntimeError(message)
    directory = (
        Path.home()
        / ".local/share/analytics-toolkit-agent/sessions"
        / sessions.repository_key(source)
    )
    root = (
        _resume_root(source, directory, args.resume_bootstrap)
        if args.resume_bootstrap
        else sessions.create(source, directory)
    )
    print(f"Session: {root.name}\nClone: {root}", flush=True)
    try:
        with bootstrap_process.lock(root / ".git/atk-bootstrap.lock", wait=False):
            # Validate again after locking to prevent concurrent resume/launch races.
            _resume_root(source, directory, root.name)
            if args.resume_bootstrap:
                # Refresh old failed clones before importing their bootstrap implementation.
                refreshed = sessions.load(root)
                refreshed["base"] = sessions.sync_dev(root)
                sessions.save(root, refreshed)
            machine_signing.configure(root, machine_signing.ensure_key(Path.home()))
            _prepare(source, root, refresh=args.refresh_bootstrap_cache, runtime=args.runtime)
            state = sessions.load(root)
            state.update(
                phase="planning",
                base=sessions.run(root, "git", "rev-parse", "HEAD"),
                runtime=args.runtime,
                reviewer=reviewer,
            )
            sessions.save(root, state)
    except (OSError, RuntimeError, subprocess.CalledProcessError, KeyboardInterrupt) as exc:
        print(
            f"Bootstrap stopped: {exc}\nResume from {source}: "
            f"{args.runtime} --resume-bootstrap {root.name}",
            file=sys.stderr,
        )
        return 130 if isinstance(exc, KeyboardInterrupt) else 1
    print("Starting native Plan mode.", flush=True)
    if args.runtime == "claude":
        return launch_claude(args.executable, root)
    return launch(args.executable, root)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
