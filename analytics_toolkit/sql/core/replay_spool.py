"""Run-owned, bounded-memory exact replay input. Never uses pickle."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import struct
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from .exact_values import decode_value, encode_value

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Sequence
    from typing import BinaryIO

_MAGIC = b"ATK-ROWS\x01\n"
_FRAME = struct.Struct(">Q")
_MANIFEST_VERSION = 1


@dataclass(frozen=True)
class FrozenInputIdentity:
    source_key: str = ""
    configuration_digest: str = ""
    namespace: tuple[str, ...] = ()
    availability_scope: tuple[str, ...] = ()
    query_digest: str = ""


class ReplaySpool:
    """Certify the full selected generation before any destination publication.

    Ownership is explicit: close only after validation/retry completion. An
    interrupted writer is never replayable and its context manager removes it.
    A certified spool can be replayed repeatedly without re-querying a source.
    """

    def __init__(self, columns: Sequence[str], identity: FrozenInputIdentity | None = None) -> None:
        self.columns = tuple(columns)
        self.identity = identity or FrozenInputIdentity()
        self._directory: tempfile.TemporaryDirectory[str] | None = tempfile.TemporaryDirectory(
            prefix="atk-exact-input-"
        )
        self.path = Path(self._directory.name) / "rows.atk"
        self._file: BinaryIO = self.path.open("w+b")
        self._file.write(_MAGIC)
        self._digest = hashlib.sha256()
        self.row_count = 0
        self.certified = False
        self.closed = False
        self.digest: str | None = None
        self._retained_manifest: Path | None = None
        self._preserve_directory = False
        self.recovery_metadata: dict[str, Any] = {}

    def write_rows(self, rows: Iterable[Sequence[Any]]) -> None:
        if self.certified or self.closed:
            msg = "A certified or closed replay generation is immutable."
            raise ValueError(msg)
        try:
            for row in rows:
                payload = self._encode_row(row)
                frame = _FRAME.pack(len(payload)) + payload
                self._file.write(frame)
                self._digest.update(frame)
                self.row_count += 1
        except BaseException:
            self._close_quietly()
            raise

    def _encode_row(self, row: Sequence[Any]) -> bytes:
        if len(row) != len(self.columns):
            msg = "Replay row width differs from frozen input columns."
            raise ValueError(msg)
        return json.dumps(
            encode_value(tuple(row)), ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")

    def certify(self) -> ReplaySpool:
        if self.closed:
            msg = "Cannot certify an incomplete replay generation."
            raise ValueError(msg)
        if self.certified:
            return self
        self._file.flush()
        os.fsync(self._file.fileno())
        self.digest = self._digest.hexdigest()
        # Verify the replacement checkpoint before a native source stage can
        # be released. Completion count alone does not certify written bytes.
        if sum(1 for _ in self._read_rows()) != self.row_count:
            msg = "Replay checkpoint row count did not validate."
            raise ValueError(msg)
        self.certified = True
        return self

    def _read_rows(self) -> Iterator[tuple[Any, ...]]:
        digest = hashlib.sha256()
        with self.path.open("rb") as stream:
            if stream.read(len(_MAGIC)) != _MAGIC:
                msg = "Unsupported exact replay spool version."
                raise ValueError(msg)
            while True:
                header = stream.read(_FRAME.size)
                if not header:
                    break
                if len(header) != _FRAME.size:
                    msg = "Truncated replay frame length."
                    raise ValueError(msg)
                size = _FRAME.unpack(header)[0]
                # Read at most the certified file's remaining size. A corrupt
                # length must not cause a giant allocation or accept truncation.
                if size > self.path.stat().st_size - stream.tell():
                    msg = "Truncated replay frame payload."
                    raise ValueError(msg)
                payload = stream.read(size)
                digest.update(header + payload)
                row = decode_value(json.loads(payload))
                if not isinstance(row, tuple) or len(row) != len(self.columns):
                    msg = "Invalid replay row shape."
                    raise ValueError(msg)
                yield row
        if self.digest is not None and digest.hexdigest() != self.digest:
            msg = "Replay checkpoint digest did not validate."
            raise ValueError(msg)

    def iter_rows(self) -> Iterator[tuple[Any, ...]]:
        if not self.certified or self.closed:
            msg = "Only a certified complete input can be replayed."
            raise ValueError(msg)
        yield from self._read_rows()

    def iter_batches(self, batch_size: int) -> Iterator[Any]:
        from analytics_toolkit.sql.dml.transfer.runtime.models import (  # noqa: PLC0415 - avoid backend registry import cycles
            RowBatch,
        )

        if batch_size < 1:
            msg = "Replay batch size must be positive."
            raise ValueError(msg)
        rows: list[tuple[Any, ...]] = []
        for row in self.iter_rows():
            rows.append(row)
            if len(rows) == batch_size:
                yield RowBatch(columns=list(self.columns), rows=rows)
                rows = []
        if rows:
            yield RowBatch(columns=list(self.columns), rows=rows)

    def retain(self, metadata: dict[str, Any]) -> str:
        """Retain a certified input after unsafe mutation, with schema-only metadata."""
        if self._retained_manifest is not None:
            return str(self._retained_manifest)
        if not self.certified or self.closed:
            msg = "Only a certified complete replay checkpoint can be retained."
            raise ValueError(msg)
        self._preserve_directory = True
        directory = cast("tempfile.TemporaryDirectory[str]", self._directory)
        finalizer = getattr(directory, "_finalizer")  # noqa: B009 - Python 3.8 retention compatibility
        finalizer.detach()
        self._file.flush()
        os.fsync(self._file.fileno())
        self._verify_count()
        manifest = self.path.parent / "manifest.json"
        self.recovery_metadata = _manifest_metadata(metadata)
        payload = {
            "version": _MANIFEST_VERSION,
            "encoding_version": 1,
            "data_file": self.path.name,
            "columns": list(self.columns),
            "identity": asdict(self.identity),
            "sha256": self.digest,
            "row_count": self.row_count,
            "metadata": self.recovery_metadata,
        }
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=self.path.parent,
            prefix="manifest-",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(manifest)
        self._retained_manifest = manifest
        try:
            self._file.close()
        except Exception as exc:  # noqa: BLE001 - owned close cannot mask unsafe mutation
            self._warn_cleanup(exc)
        return str(manifest)

    @classmethod
    def open_retained(cls, manifest_path: str | Path) -> ReplaySpool:
        """Validate the entire retained generation before authorizing replay."""
        manifest = Path(manifest_path).resolve()
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        if payload.get("version") != _MANIFEST_VERSION or payload.get("encoding_version") != 1:
            msg = "Unsupported retained replay checkpoint version."
            raise ValueError(msg)
        if payload.get("data_file") != "rows.atk":
            msg = "Invalid retained replay checkpoint data path."
            raise ValueError(msg)
        identity = payload["identity"]
        self = cls.__new__(cls)
        self.columns = tuple(payload["columns"])
        self.identity = FrozenInputIdentity(
            source_key=identity["source_key"],
            configuration_digest=identity["configuration_digest"],
            namespace=tuple(identity["namespace"]),
            availability_scope=tuple(identity["availability_scope"]),
            query_digest=identity["query_digest"],
        )
        self._directory = None
        self.path = manifest.parent / "rows.atk"
        self._file = self.path.open("rb")
        self._digest = hashlib.sha256()
        self.row_count = int(payload["row_count"])
        self.digest = str(payload["sha256"])
        self.closed = False
        self.certified = False
        self._retained_manifest = manifest
        self._preserve_directory = True
        self.recovery_metadata = payload["metadata"]
        try:
            self._verify_count()
        except BaseException:
            self._close_quietly()
            raise
        self.certified = True
        return self

    def _verify_count(self) -> None:
        if sum(1 for _ in self._read_rows()) != self.row_count:
            msg = "Replay checkpoint row count did not validate."
            raise ValueError(msg)

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.certified = False
            try:
                self._file.close()
            except Exception as exc:  # noqa: BLE001 - owned cleanup cannot replay or mask work
                self._warn_cleanup(exc)
            finally:
                try:
                    if self._directory is not None and not self._preserve_directory:
                        self._directory.cleanup()
                except Exception as exc:  # noqa: BLE001 - owned cleanup cannot replay or mask work
                    self._warn_cleanup(exc)

    @staticmethod
    def _warn_cleanup(exc: BaseException) -> None:
        logging.getLogger(__name__).warning(
            "Owned replay checkpoint cleanup failed (%s).", type(exc).__name__
        )

    def _close_quietly(self) -> None:
        try:
            self.close()
        except Exception as exc:  # noqa: BLE001 - owned cleanup cannot replay or mask work
            self._warn_cleanup(exc)

    def __enter__(self) -> ReplaySpool:  # noqa: PYI034 - Python 3.8 lacks typing.Self
        return self

    def __exit__(self, *args: object) -> None:
        self._close_quietly()


def _manifest_metadata(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _manifest_metadata(item)
            for key, item in value.items()
            if str(key).casefold()
            not in {"password", "passwd", "pwd", "secret", "token", "credentials", "authorization"}
        }
    if isinstance(value, (list, tuple)):
        return [_manifest_metadata(item) for item in value]
    return value
