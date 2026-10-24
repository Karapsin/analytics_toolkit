"""Read-only, immutable Git objects for a fresh local source reviewer."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import subprocess
import sys
from pathlib import Path

PAGE = 24000


class Files:
    def __init__(self, root: Path, head: str, base: str, ledger: Path) -> None:
        self.root, self.head, self.base, self.ledger = root, head, base, ledger
        self.paths = self.git("ls-tree", "-rz", "--name-only", head).decode().split("\0")[:-1]
        self.base_paths = self.git("ls-tree", "-rz", "--name-only", base).decode().split("\0")[:-1]
        self.changed = (
            self.git(
                "diff",
                "--no-ext-diff",
                "--no-textconv",
                "--no-renames",
                "--name-only",
                "-z",
                base,
                head,
            )
            .decode()
            .split("\0")[:-1]
        )

    def git(self, *args: str) -> bytes:
        return subprocess.run(
            ["git", "--no-replace-objects", "-c", "core.hooksPath=/dev/null", *args],
            cwd=self.root,
            capture_output=True,
            check=True,
        ).stdout

    def note(self, value: dict) -> None:
        with self.ledger.open("a") as output:
            output.write(json.dumps(value) + "\n")

    def changed_files(self) -> list[str]:
        """Enumerate the complete changed-file inventory, including deletions."""
        self.note({"kind": "inventory", "paths": self.changed})
        return self.changed

    def list_files(self, prefix: str = "") -> list[str]:
        """List candidate paths without reading working files or ignored state."""
        return [path for path in self.paths if path.startswith(prefix)]

    def read_file(self, path: str, offset: int = 0) -> dict:
        """Read the next bounded character page of an immutable UTF-8 blob."""
        if path not in self.paths or ".connections" in Path(path).parts:
            msg = "Path is not a candidate Git blob"
            raise ValueError(msg)
        return self.page(
            self.git("cat-file", "blob", self.head + ":" + path).decode(), path, offset, "source"
        )

    def read_diff(self, path: str, offset: int = 0) -> dict:
        """Read every page for each changed path before deciding a review."""
        if path not in self.changed or ".connections" in Path(path).parts:
            msg = "Path is not in the changed-file inventory"
            raise ValueError(msg)
        text = self.git(
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--no-renames",
            self.base,
            self.head,
            "--",
            ":(literal)" + path,
        ).decode()
        return self.page(text, path, offset, "diff")

    def read_base_file(self, path: str, offset: int = 0) -> dict:
        """Read existing instructions from the immutable dev base, not candidate policy."""
        if path not in self.base_paths or ".connections" in Path(path).parts:
            msg = "Path is not a base Git blob"
            raise ValueError(msg)
        return self.page(
            self.git("cat-file", "blob", self.base + ":" + path).decode(), path, offset, "base"
        )

    def page(self, text: str, path: str, offset: int, kind: str) -> dict:
        if offset < 0 or offset > len(text):
            msg = "Page offset is outside the immutable content"
            raise ValueError(msg)
        end = min(len(text), offset + PAGE)
        value = {
            "kind": kind,
            "path": path,
            "offset": offset,
            "end": end,
            "total": len(text),
            "sha256": hashlib.sha256(text.encode()).hexdigest(),
        }
        self.note(value)
        return {
            **value,
            "content": text[offset:end],
            "next_offset": end if end < len(text) else None,
        }

    def read_image(self, path: str) -> dict:
        """Return an original tracked PNG without image processing."""
        if (
            path not in self.paths
            or not path.endswith(".png")
            or ".connections" in Path(path).parts
        ):
            msg = "Expected a tracked PNG"
            raise ValueError(msg)
        data = self.git("cat-file", "blob", self.head + ":" + path)
        if len(data) > 8 * 1024 * 1024 or not data.startswith(b"\x89PNG\r\n\x1a\n"):
            msg = "Invalid or oversized PNG"
            raise ValueError(msg)
        self.note({"kind": "image", "path": path, "sha256": hashlib.sha256(data).hexdigest()})
        return {"mime_type": "image/png", "base64": base64.b64encode(data).decode()}


def complete_coverage(files: Files) -> None:
    events = [json.loads(line) for line in files.ledger.read_text().splitlines()]
    if not any(
        event.get("kind") == "inventory" and event.get("paths") == files.changed for event in events
    ):
        msg = "Reviewer did not enumerate the complete patch"
        raise ValueError(msg)
    for path in files.changed:
        if ".connections" in Path(path).parts:
            msg = "Sensitive connection files cannot enter local review"
            raise ValueError(msg)
        expected = files.git(
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--no-renames",
            files.base,
            files.head,
            "--",
            ":(literal)" + path,
        ).decode()
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
        if stats.startswith(b"-\t-\t") and path in files.paths:
            data = files.git("cat-file", "blob", files.head + ":" + path)
            digest = hashlib.sha256(data).hexdigest()
            if not any(
                event.get("kind") == "image"
                and event.get("path") == path
                and event.get("sha256") == digest
                for event in events
            ):
                msg = (
                    "Changed binary requires a reviewed PNG; unsupported formats block publication"
                )
                raise ValueError(msg)
        digest = hashlib.sha256(expected.encode()).hexdigest()
        pages = sorted(
            (event["offset"], event["end"])
            for event in events
            if event.get("kind") == "diff"
            and event.get("path") == path
            and event.get("sha256") == digest
            and event.get("total") == len(expected)
        )
        cursor = 0
        for start, end in pages:
            if start > cursor:
                break
            cursor = max(cursor, end)
        if not pages or cursor != len(expected):
            raise ValueError("Reviewer did not inspect the complete diff: " + path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--verify-ledger", action="store_true")
    args = parser.parse_args()
    files = Files(args.root, args.head, args.base, args.ledger)
    # MCP stdio is newline-delimited JSON-RPC. This provider deliberately has
    # no dependency on project imports, user MCP configuration or shell tools.
    if args.verify_ledger:
        complete_coverage(files)
    else:
        serve(files)


def catalog(files: Files) -> tuple[list, dict]:
    tools = []
    methods = {
        name: getattr(files, name)
        for name in (
            "changed_files",
            "list_files",
            "read_file",
            "read_base_file",
            "read_diff",
            "read_image",
        )
    }
    for name, method in methods.items():
        properties = {}
        required = []
        if name in {"read_file", "read_base_file", "read_diff", "read_image"}:
            properties["path"] = {"type": "string"}
            required.append("path")
        if name in {"read_file", "read_base_file", "read_diff"}:
            properties["offset"] = {"type": "integer", "minimum": 0}
        if name == "list_files":
            properties["prefix"] = {"type": "string"}
        tools.append(
            {
                "name": name,
                "description": method.__doc__,
                "annotations": {"readOnlyHint": True},
                "inputSchema": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                    "additionalProperties": False,
                },
            }
        )
    return tools, methods


def serve(files: Files) -> None:
    tools, methods = catalog(files)
    for line in sys.stdin:
        request = json.loads(line)
        if "id" not in request:
            continue
        method, params = request.get("method"), request.get("params", {})
        if method == "initialize":
            result = {
                "protocolVersion": params["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "local-review-source", "version": "1"},
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": tools}
        elif method == "tools/call":
            try:
                value = methods[params["name"]](**params.get("arguments", {}))
                content = (
                    [{"type": "image", "mimeType": value["mime_type"], "data": value["base64"]}]
                    if params["name"] == "read_image"
                    else [{"type": "text", "text": json.dumps(value)}]
                )
                result = {"content": content}
            except (KeyError, TypeError, ValueError, subprocess.SubprocessError):
                result = {
                    "isError": True,
                    "content": [{"type": "text", "text": "Invalid immutable source request"}],
                }
        else:
            print(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": request["id"],
                        "error": {"code": -32601, "message": "Method not found"},
                    }
                ),
                flush=True,
            )
            continue
        print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)


if __name__ == "__main__":
    main()
