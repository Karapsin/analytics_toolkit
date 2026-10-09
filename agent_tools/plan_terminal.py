"""Start the native Codex Plan mode before forwarding user input."""

from __future__ import annotations

import contextlib
import os
import re
import select
import signal
import subprocess
import sys
import time
from pathlib import Path

from agent_tools import machine_signing, sessions


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
            raise RuntimeError(msg)
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
        raise RuntimeError(msg)
    stdin = sys.stdin.fileno()
    previous = termios.tcgetattr(stdin)
    pid, master = pty.fork()
    if pid == 0:
        os.chdir(root)
        os.execv(  # noqa: S606 - explicit installed CLI executable, without a shell.
            executable,
            [
                executable,
                "-C",
                str(root),
                "-m",
                "gpt-6.1-sol",
                "-c",
                'model_reasoning_effort="medium"',
                "-c",
                'plan_mode_reasoning_effort="medium"',
                "--no-alt-screen",
            ],
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


def main() -> int:
    source, executable = Path(sys.argv[1]).resolve(), sys.argv[2]
    directory = Path.home() / ".local/share/analytics-toolkit-agent/sessions"
    root = sessions.create(source, directory / sessions.repository_key(source))
    machine_signing.configure(root, machine_signing.ensure_key(Path.home()))
    python = root / ".venv/bin/python"
    subprocess.run([sys.executable, "-m", "venv", str(root / ".venv")], check=True)
    subprocess.run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "-q",
            "-r",
            str(root / "agent_tools/requirements-mcp.txt"),
        ],
        check=True,
    )
    # The clone's tools must prepare its own environment and index before Plan mode starts.
    subprocess.run(
        [
            str(root / "agent_tools/mcp_tool.sh"),
            "prepare-start",
            "--task",
            "New isolated Codex session",
            "--root",
            str(root),
        ],
        cwd=root,
        check=True,
    )
    state = sessions.load(root)
    state["phase"] = "planning"
    sessions.save(root, state)
    print(f"Session: {state['id']}\nClone: {root}\nStarting native Plan mode.", flush=True)
    return launch(executable, root)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (RuntimeError, subprocess.CalledProcessError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
