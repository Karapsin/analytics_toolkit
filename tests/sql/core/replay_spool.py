from __future__ import annotations

import struct
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

import pytest
from analytics_toolkit.sql.core.exact_values import ExactTemporal, OrderedMapPairs
from analytics_toolkit.sql.core.replay_spool import FrozenInputIdentity, ReplaySpool


def test_certified_checkpoint_replays_exact_rows_and_identity_until_released() -> None:
    rows = [
        (
            2**256 - 1,
            Decimal("12345678901234567890.123456789"),
            ExactTemporal(-1, 9, "UTC"),
            UUID(int=1),
            b"\x00\xff",
            [1, None],
            ("x", 2),
            OrderedMapPairs(((1, "a"), (1, "b"))),
        ),
        (None,) * 8,
    ]
    identity = FrozenInputIdentity(
        "source", "config-digest", ("db",), ("host:9000",), "query-digest"
    )
    with ReplaySpool([str(index) for index in range(8)], identity) as spool:
        spool.write_rows(rows)
        assert not spool.certified
        with pytest.raises(ValueError, match="certified"):
            list(spool.iter_rows())
        spool.certify()
        assert spool.identity == identity
        assert spool.row_count == 2
        assert list(spool.iter_rows()) == rows
        assert list(spool.iter_rows()) == rows
        assert [batch.rows for batch in spool.iter_batches(1)] == [[rows[0]], [rows[1]]]
        assert [batch.rows for batch in spool.iter_batches(3)] == [rows]
        assert spool.certify() is spool
        with pytest.raises(ValueError, match="immutable"):
            spool.write_rows(rows)
        with pytest.raises(ValueError, match="positive"):
            list(spool.iter_batches(0))
        path = spool.path
    assert not path.exists()
    spool.close()
    with pytest.raises(ValueError, match="incomplete"):
        spool.certify()


def test_partial_generation_or_cancellation_never_certifies_or_leaks() -> None:
    spool = ReplaySpool(["v"])
    path = spool.path

    def interrupted():
        yield (1,)
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        spool.write_rows(interrupted())
    assert not spool.certified
    assert spool.closed
    assert not path.exists()
    with ReplaySpool(["v"]) as spool:
        with pytest.raises(ValueError, match="width"):
            spool.write_rows([(1, 2)])
        assert spool.closed


def test_corrupt_checkpoint_cannot_certify_or_replay() -> None:
    with ReplaySpool(["v"]) as spool:
        spool.write_rows([(123,)])
        spool.certify()
        payload = spool.path.read_bytes()
        spool.path.write_bytes(payload[:-1])
        with pytest.raises(ValueError, match="Truncated"):
            list(spool.iter_rows())
    with ReplaySpool([]) as spool:
        spool.certify()
        assert list(spool.iter_rows()) == []
        spool.path.write_bytes(b"future-format")
        with pytest.raises(ValueError, match="version"):
            list(spool.iter_rows())


def test_spool_certification_rejects_complete_length_tampering_and_bad_frames() -> None:
    with ReplaySpool(["v"]) as spool:
        spool.write_rows([(123,)])
        spool._file.flush()
        original = spool.path.read_bytes()
        spool.path.write_bytes(original.replace(b"123", b"124"))
        with pytest.raises(ValueError, match="digest"):
            spool.certify()
    with ReplaySpool(["v"]) as spool:
        spool.write_rows([(1,)])
        spool._file.flush()
        spool.path.write_bytes(spool.path.read_bytes()[:12])
        with pytest.raises(ValueError, match="frame length"):
            spool.certify()
    with ReplaySpool(["v"]) as spool:
        spool.write_rows([(1,)])
        spool._file.flush()
        # A correctly framed JSON scalar is still not a replay row.
        payload = b'["int","1"]'
        spool.path.write_bytes(b"ATK-ROWS\x01\n" + struct.pack(">Q", len(payload)) + payload)
        with pytest.raises(ValueError, match="row shape"):
            spool.certify()
    with ReplaySpool(["v"]) as spool:
        spool.write_rows([(1,)])
        spool.row_count = 2
        with pytest.raises(ValueError, match="row count"):
            spool.certify()


def test_owned_cleanup_failure_never_masks_partial_write_or_context_failure(
    monkeypatch, caplog
) -> None:
    spool = ReplaySpool(["v"])
    close = spool.close

    def failed_close():
        message = "private checkpoint bytes must not reach logs"
        raise OSError(message)

    monkeypatch.setattr(spool, "close", failed_close)

    def rows():
        yield (1,)
        message = "primary scan failed"
        raise RuntimeError(message)

    try:
        with pytest.raises(RuntimeError, match="primary scan failed"):
            spool.write_rows(rows())
        with pytest.raises(KeyboardInterrupt), spool:
            raise KeyboardInterrupt
        assert "private checkpoint bytes" not in caplog.text
        assert "Owned replay checkpoint cleanup failed (OSError)" in caplog.text
    finally:
        close()


def test_owned_file_close_failure_does_not_turn_success_into_error(monkeypatch, caplog) -> None:
    spool = ReplaySpool(["v"])
    spool.write_rows([(42,)])
    spool.certify()
    spool._file.close()

    def failed_close():
        message = "private bytes"
        raise OSError(message)

    monkeypatch.setattr(spool, "_file", SimpleNamespace(close=failed_close))
    spool.close()
    assert spool.closed
    assert not spool.path.exists()
    assert "private bytes" not in caplog.text


def test_retained_checkpoint_survives_cleanup_and_validates_before_reopening(tmp_path, monkeypatch):
    import gc  # noqa: PLC0415 - recovery lifetime regression
    import json  # noqa: PLC0415 - manifest inspection
    import tempfile  # noqa: PLC0415 - private test checkpoint directory
    from pathlib import Path  # noqa: PLC0415 - recovery path

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    spool = ReplaySpool(["id"], FrozenInputIdentity(query_digest="query-hash"))
    spool.write_rows([(2**256 - 1,), (None,)])
    spool.certify()
    manifest = Path(spool.retain({"types": ["Int256"], "password": "private"}))
    assert spool.retain({}) == str(manifest)
    spool.close()
    del spool
    gc.collect()
    assert manifest.exists()
    payload = json.loads(manifest.read_text())
    assert payload["metadata"] == {"types": ["Int256"]}
    with ReplaySpool.open_retained(manifest) as recovered:
        assert recovered.certified
        assert recovered.identity.query_digest == "query-hash"
        assert list(recovered.iter_rows()) == [(2**256 - 1,), (None,)]
    assert manifest.exists()
    payload["row_count"] += 1
    manifest.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="row count"):
        ReplaySpool.open_retained(manifest)
    payload["row_count"] -= 1
    payload["sha256"] = "invalid"
    manifest.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="digest"):
        ReplaySpool.open_retained(manifest)
    payload["version"] = 999
    manifest.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="version"):
        ReplaySpool.open_retained(manifest)
    payload["version"] = 1
    payload["data_file"] = "../rows.atk"
    manifest.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="path"):
        ReplaySpool.open_retained(manifest)
    with ReplaySpool(["id"]) as incomplete, pytest.raises(ValueError, match="certified"):
        incomplete.retain({})


def test_failed_retention_preserves_certified_bytes_for_structural_recovery(tmp_path, monkeypatch):
    import os  # noqa: PLC0415 - inject post-certification durability failure
    import tempfile  # noqa: PLC0415 - private test checkpoint directory

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    spool = ReplaySpool(["id"])
    spool.write_rows([(1,)])
    spool.certify()
    path = spool.path

    def fail(_descriptor):
        msg = "durability failure"
        raise OSError(msg)

    monkeypatch.setattr(os, "fsync", fail)
    with pytest.raises(OSError, match="durability"):
        spool.retain({})
    spool.close()
    assert path.exists()


def test_recovery_and_temporary_directory_close_faults_are_sanitized(tmp_path, monkeypatch, caplog):
    import tempfile  # noqa: PLC0415 - private checkpoint ownership

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))

    def failed_close():
        msg = "private checkpoint content"
        raise OSError(msg)

    spool = ReplaySpool(["v"])
    spool.write_rows([(42,)])
    spool.certify()
    writer = spool._file
    monkeypatch.setattr(
        spool,
        "_file",
        SimpleNamespace(flush=writer.flush, fileno=writer.fileno, close=failed_close),
    )
    manifest = spool.retain({"nested": {"types": ["BIGINT"]}})
    with ReplaySpool.open_retained(manifest) as recovered:
        assert list(recovered.iter_rows()) == [(42,)]
    spool.close()
    writer.close()
    assert "private checkpoint content" not in caplog.text
    with ReplaySpool(["v"]) as owned:
        cleanup = owned._directory.cleanup
        monkeypatch.setattr(owned._directory, "cleanup", failed_close)
    assert "private checkpoint content" not in caplog.text
    cleanup()
