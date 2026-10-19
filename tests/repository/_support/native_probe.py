"""Service-free boundaries for the actual fixed native-probe programs."""

from __future__ import annotations

import builtins
import errno
import importlib.metadata
import io
import json
import socket
import struct
import sys
import threading
import zlib
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from tests.repository.github_agent import controller

if TYPE_CHECKING:
    from pathlib import Path

probe = controller.trusted_module("capture_isolation_probe")
isolation = controller.trusted_module("capture_isolation")
artifact = controller.trusted_module("capture_artifact")


class Sandbox:
    def __init__(self, state: Any, fault: str, candidate: Path, *_args: Any) -> None:
        self.state, self.fault = state, fault
        self.state.candidate = candidate
        self.image = "image-digest"
        self.network_id = "owned-network"
        self.containers: list[str] = []
        self.rules = ["owned-rule"]

    def setup(self) -> None:
        pass

    def create(self, phase: str, *_args: Any) -> str:
        self.containers.append(phase)
        return phase

    def verify(self) -> None:
        pass

    def validate_container(self, _container: str) -> None:
        pass

    def remove(self, container: str) -> None:
        self.containers.remove(container)

    def cleanup(self) -> None:
        self.state.cleanup = True
        if self.fault == "cleanup":
            message = "cleanup refused"
            raise ValueError(message)
        if self.fault == "remaining":
            self.containers.append("survivor")
        self.rules.clear()

    def uncertain_operations(self) -> list[str]:
        return ["uncertain-create"] if self.fault == "uncertain" else []


class Listener:
    def __init__(self, state: Any) -> None:
        self.state = state

    def bind(self, *_args: Any) -> None:
        pass

    def listen(self, *_args: Any) -> None:
        pass

    def settimeout(self, *_args: Any) -> None:
        pass

    def getsockname(self) -> tuple[str, int]:
        return ("0.0.0.0", 10000)

    def accept(self) -> Any:
        if self.state.cleanup or self.state.counter_reads > 1:
            raise TimeoutError
        return (SimpleNamespace(close=lambda: None), None)

    def close(self) -> None:
        pass


class Connection:
    def __init__(self, fault: str) -> None:
        self.fault = fault

    def __enter__(self) -> Any:
        return self

    def __exit__(self, *_args: object) -> None:
        pass

    def settimeout(self, _value: int) -> None:
        pass

    def connect(self, _address: Any) -> None:
        if self.fault != "network":
            raise OSError(errno.ENETUNREACH, "fixture has no route")


class ImageFixture:
    """Controlled Pillow boundary, paired with a valid fixture PNG."""

    def __init__(self, size: tuple[int, int], level: int) -> None:
        self.size, self.extrema = size, [(level, level)] * 3

    def __enter__(self) -> Any:
        return self

    def __exit__(self, *_args: object) -> None:
        pass

    def convert(self, _mode: str) -> Any:
        return self


def png_fixture(size: tuple[int, int], level: int) -> bytes:
    def chunk(kind: bytes, body: bytes) -> bytes:
        return (
            struct.pack("!I", len(body)) + kind + body + struct.pack("!I", zlib.crc32(kind + body))
        )

    header = struct.pack("!2I5B", *size, 8, 2, 0, 0, 0)
    rows = (b"\0" + bytes([level]) * (size[0] * 3)) * size[1]
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


def pillow_fixture(size: tuple[int, int], level: int) -> Any:
    def inspect_font(_path: str) -> None:
        message = "Unparseable inspection fixture"
        raise OSError(message)

    return SimpleNamespace(
        __version__="fixture-pillow-version",
        Image=SimpleNamespace(open=lambda _stream: ImageFixture(size, level)),
        ImageStat=SimpleNamespace(Stat=lambda image: SimpleNamespace(extrema=image.extrema)),
        ImageFont=SimpleNamespace(truetype=inspect_font),
    )


def configure_capture(tmp_path: Path, monkeypatch: Any, fault: str) -> Any:
    output = tmp_path / "probe-evidence"
    fonts = tmp_path / "fonts"
    fonts.mkdir()
    if fault != "fonts":
        (fonts / "fixture.ttf").write_bytes(b"unparseable inspection fixture")
    net = tmp_path / "interfaces"
    net.mkdir()
    (net / "lo").mkdir()
    dimensions = (2, 2) if fault == "size" else (1280, 800)
    level = 0 if fault == "black" else 255
    png = png_fixture(dimensions, level)
    calls: list[dict[str, Any]] = []

    def render(**options: Any) -> bytes:
        calls.append(options)
        if fault == "render":
            message = "actual renderer boundary failed"
            raise ValueError(message)
        return png

    roots = {"/work/probe-evidence": output, "/usr/share/fonts": fonts, "/sys/class/net": net}
    monkeypatch.setitem(sys.modules, "pathlib", SimpleNamespace(Path=lambda value: roots[value]))
    monkeypatch.setitem(sys.modules, "resvg_py", SimpleNamespace(svg_to_bytes=render))
    monkeypatch.setitem(sys.modules, "PIL", pillow_fixture(dimensions, level))
    monkeypatch.setitem(
        sys.modules,
        "socket",
        SimpleNamespace(
            AF_INET=socket.AF_INET,
            AF_INET6=socket.AF_INET6,
            SOCK_STREAM=socket.SOCK_STREAM,
            socket=lambda *_args: Connection(fault),
        ),
    )
    monkeypatch.setattr(importlib.metadata, "version", lambda _name: "fixture-renderer-version")
    monkeypatch.setattr(sys, "argv", ["capture", '{"gateway":"192.0.2.1"}'])
    original_import = builtins.__import__

    def importing(name: str, *args: Any, **kwargs: Any) -> Any:
        if fault == "import" and name == "PIL":
            message = "actual optional import failed"
            raise ImportError(message)
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", importing)
    return output, fonts, dimensions, png, calls


def configure_main(tmp_path: Path, monkeypatch: Any, fault: str) -> Any:
    state = SimpleNamespace(candidate=None, cleanup=False, counter_reads=0)

    fake_module = SimpleNamespace(
        IMAGES={"3.11": "fixture"},
        Sandbox=lambda *args: Sandbox(state, fault, *args),
        command=lambda _args: '[{"IPAM":{"Config":[{"Gateway":"192.0.2.1"}]}}]',
    )
    writer = threading.Event()
    if fault == "writer":
        writer.set()

    def contained(_container: str, phase: str, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        if fault != "missing":
            folder = state.candidate / "probe-evidence"
            folder.mkdir(exist_ok=True)
            (folder / (phase + ".json")).write_text(json.dumps({"phase": phase}))
        return {
            "ok": not (fault == "install" and phase == "probe")
            and not (fault in {"capture", "capture-counter"} and phase == "capture")
        }

    def collecting(source: Path, destination: Path, module: Any) -> None:
        assert state.cleanup
        isolation.collect(source, destination, module)

    fake_isolation = SimpleNamespace(
        trusted_module=lambda name: fake_module if name == "capture_sandbox" else artifact,
        contained_output=contained,
        collect=collecting,
        UNSAFE_LOG_WRITER=writer,
    )
    monkeypatch.setattr(
        probe.importlib.util,
        "spec_from_file_location",
        lambda *_a: SimpleNamespace(loader=SimpleNamespace(exec_module=lambda _module: None)),
    )
    monkeypatch.setattr(probe.importlib.util, "module_from_spec", lambda _spec: fake_isolation)

    def counters(*_args: Any) -> dict[str, int]:
        state.counter_reads += 1
        if fault in {"counter", "capture-counter"} and state.counter_reads > 1:
            message = "counter response unavailable"
            raise ValueError(message)
        return {
            "-A INPUT -i owned-bridge ": state.counter_reads,
            "-A owned -d 169.254.0.0/16 ": state.counter_reads,
        }

    monkeypatch.setattr(probe, "owned_counters", counters)
    monkeypatch.setattr(probe.socket, "socket", lambda: Listener(state))
    monkeypatch.setattr(probe.socket, "create_connection", lambda *_a, **_k: io.BytesIO())
    monkeypatch.setattr(probe.signal, "signal", lambda *_a: None)
    monkeypatch.setattr(probe.signal, "alarm", lambda *_a: None)
    command_file = tmp_path / "commands"
    command_file.touch()
    for key, value in {
        "PROBE_PYTHON": "3.11",
        "RUNNER_TEMP": str(tmp_path),
        "GITHUB_SHA": "a" * 40,
        "GITHUB_REPOSITORY": "owner/repo",
        "GITHUB_RUN_ID": "1",
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_OUTPUT": str(command_file),
    }.items():
        monkeypatch.setenv(key, value)
    return state, command_file
