"""Retain certified checkpoints when the selected SDK rejects publication."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from analytics_toolkit.sql.backends.ch.client_validation import ClientWriteValidationError
from analytics_toolkit.sql.backends.models import SourceColumn
from analytics_toolkit.sql.core.replay_spool import FrozenInputIdentity, ReplaySpool
from analytics_toolkit.sql.core.type_resolution import (
    ColumnProfile,
    ResolvedColumn,
    resolve_profile,
)
from analytics_toolkit.sql.dml.transfer.flow import client_validation
from analytics_toolkit.sql.dml.transfer.flow.adaptive_input import AdaptiveTransferContext
from analytics_toolkit.sql.dml.transfer.runtime.models import TransferOptions, TransferStageState

from tests.sql.backends.ch._support.exact_transport import native_context

_INVALID_ENCODING = "atk-unavailable-codec-9012"


def _options(*, to_db_key="ch"):
    return TransferOptions(
        from_db_key="ch",
        from_db_backend="ch",
        to_db_key=to_db_key,
        to_db_backend="ch",
        source_sql="SELECT value FROM source",
        target_table="sandbox.target",
        batch_size=2,
        min_batch_size=1,
        max_batch_size=2,
    )


@pytest.fixture
def complete_string_checkpoint():
    schema = {"value": "SimpleAggregateFunction(any, String)"}
    values = ["private-alpha", "private-beta"]
    profile = ColumnProfile(native_type="Nullable(String)", source_backend="ch")
    for value in values:
        profile.add(value)
    resolution = resolve_profile(
        profile,
        "ch",
        source_type="Nullable(String)",
        source_backend="ch",
        authoritative_type=schema["value"],
    )
    assert [resolution.convert(value) for value in values] == values
    options = _options(to_db_key="ch_target")
    with ReplaySpool(["value"], identity=FrozenInputIdentity(source_key="ch")) as spool:
        spool.write_rows([(value,) for value in values])
        spool.certify()
        options.input_resolution.update(spool=spool, resolutions=[resolution])
        yield SimpleNamespace(options=options, schema=schema, spool=spool, values=values)


@pytest.fixture
def actual_native_encoding(monkeypatch):
    from clickhouse_driver.columns.stringcolumn import (  # noqa: PLC0415 -- instrument the actual installed encoder.
        String,
    )

    original = String.write_items
    attempted_columns = []
    encoder_failures = []
    encodings = []

    def tracked_encoder(column, items, destination):
        attempted_columns.append(tuple(items))
        encodings.append(column.encoding)
        try:
            return original(column, items, destination)
        except Exception as error:
            encoder_failures.append(type(error).__name__)
            raise

    monkeypatch.setattr(String, "write_items", tracked_encoder)
    return SimpleNamespace(
        attempted_columns=attempted_columns, failures=encoder_failures, encodings=encodings
    )


@pytest.mark.parametrize("native_snapshot", [False, True], ids=["local-only", "native-replacement"])
def test_client_checkpoint_retention_after_actual_sdk_batch_failure(
    monkeypatch, native_snapshot, complete_string_checkpoint, actual_native_encoding
):
    checkpoint = complete_string_checkpoint
    options, values, spool = checkpoint.options, checkpoint.values, checkpoint.spool
    snapshot = ("source_stage.owned_source_9012", 2, "source-generation", {0: 2})
    if native_snapshot:
        options.input_resolution["native_snapshot"] = snapshot
    closed = []
    mutations = []
    target_context = native_context()
    target_context.client_settings = {
        **target_context.client_settings,
        "strings_encoding": _INVALID_ENCODING,
    }
    assert target_context.client_settings["strings_encoding"] == _INVALID_ENCODING

    def unexpected_target_write(*_args, **_kwargs):
        mutations.append(True)
        pytest.fail("client preflight must not mutate the target")

    connection = SimpleNamespace(
        is_native_transport=True,
        context=target_context,
        effective_write_timezone="UTC",
        close=lambda: closed.append(True),
        command=unexpected_target_write,
        insert=unexpected_target_write,
    )
    assert client_validation.prepare_client_write_validation(
        options,
        connection,
        [SourceColumn("value", "Nullable(String)")],
        checkpoint.schema,
        authoritative=True,
    )
    messages = []
    monkeypatch.setattr(
        client_validation, "time_print", lambda message, **_kwargs: messages.append(message)
    )
    opened = []

    def open_connection(alias):
        opened.append(alias)
        return connection

    context = AdaptiveTransferContext(1, 1, open_connection=open_connection)
    with pytest.raises(ClientWriteValidationError, match="native encoder") as caught:
        client_validation.certify_frozen_client_input(options, context)
    assert tuple(values) in actual_native_encoding.attempted_columns
    assert actual_native_encoding.failures == ["LookupError"]
    assert actual_native_encoding.encodings == [_INVALID_ENCODING]
    assert "private-alpha" not in str(caught.value)
    assert "private-beta" not in str(caught.value)
    assert caught.value.analytics_toolkit_sql_retry_safe is False
    assert spool.certified
    assert spool.row_count == 2
    assert list(spool.iter_rows()) == [(value,) for value in values]
    assert "client_write_certificate" not in options.input_resolution
    assert opened == ["ch_target"]
    assert closed == [True]
    assert mutations == []
    if native_snapshot:
        assert options.input_resolution["native_snapshot"] is snapshot
        assert messages == [
            "Retained source checkpoint source_stage.owned_source_9012: "
            "selected client writer was not certified."
        ]
    else:
        assert "native_snapshot" not in options.input_resolution
        assert messages == []
    assert all("private-" not in message for message in messages)


def test_certified_empty_checkpoint_restart_never_constructs_a_client_codec(monkeypatch):
    options = _options()
    schema = {"value": "Object('json')"}
    spool = ReplaySpool(["value"])
    spool.certify()
    options.input_resolution.update(
        spool=spool,
        resolutions=[ResolvedColumn(schema["value"], target_backend="ch")],
    )

    def unexpected_codec(*_args, **_kwargs):
        pytest.fail("a certified empty input has no future client write")

    monkeypatch.setattr(client_validation, "validate_client_write_capabilities", unexpected_codec)
    state = TransferStageState(
        target_exists=False, source_columns=["value"], stage_column_types=schema
    )
    try:
        # No client plan is required for SQL-only execution, even on restart.
        client_validation.require_client_write_certificate(options, object(), state)
        assert not client_validation.prepare_client_write_validation(
            options,
            object(),
            [SourceColumn("value", "Object('json')")],
            schema,
            authoritative=True,
        )
        context = AdaptiveTransferContext(1, 1, open_connection=unexpected_codec)
        client_validation.certify_frozen_client_input(options, context)
        client_validation.require_client_write_certificate(options, object(), state)
        assert options.input_resolution["client_write_plan"]["empty_input"] is True
        assert "client_write_certificate" not in options.input_resolution
        assert spool.certified
        assert spool.row_count == 0
    finally:
        spool.close()
