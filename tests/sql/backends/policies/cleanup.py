from __future__ import annotations

from tests.sql._support.policies import (
    SimpleNamespace,
    parquet_stage_module,
)


def test_parquet_storage_credentials_reach_upload_and_cleanup() -> None:
    open_calls: list[tuple[str, str, dict[str, str]]] = []
    cleanup_calls: list[tuple[str, dict[str, str]]] = []

    class RemoteFile:
        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def write(self, value: bytes) -> int:
            return len(value)

    class FileSystem:
        def rm(self, path: str, *, recursive: bool) -> None:
            assert recursive is True
            cleanup_calls.append((path, {}))

    fs = FileSystem()
    fsspec = SimpleNamespace(
        open=lambda uri, mode, **kwargs: open_calls.append((uri, mode, kwargs)) or RemoteFile(),
        core=SimpleNamespace(
            url_to_fs=lambda uri, **kwargs: (
                cleanup_calls.append((uri, kwargs)) or (fs, "bucket/stage")
            )
        ),
    )
    options = {"key": "access-value", "secret": "secret-value"}

    parquet_stage_module.upload_spooled_file(
        fsspec,
        SimpleNamespace(read=lambda _size=-1: b""),
        "s3://bucket/stage/file.parquet",
        storage_options=options,
    )
    parquet_stage_module.cleanup_parquet_stage_location(
        "s3://bucket/stage/",
        fsspec_module=fsspec,
        storage_options=options,
    )

    assert open_calls == [
        ("s3://bucket/stage/file.parquet", "wb", options),
    ]
    assert cleanup_calls[0] == ("s3://bucket/stage/", options)
