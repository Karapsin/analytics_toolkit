from __future__ import annotations

from tests.sql._support.row_batches import (
    Decimal,
    keys_module,
    pytest,
)


def test_key_normalization_empty_invalid_cartesian_literals_and_null_predicate() -> None:
    with pytest.raises(ValueError, match="at least one placeholder"):
        keys_module.normalize_transfer_keys([])
    with pytest.raises(ValueError, match="mapping keys"):
        keys_module.normalize_transfer_keys({1: "id"})
    with pytest.raises(ValueError, match="mapping values"):
        keys_module.normalize_transfer_keys({"id": 1})
    with pytest.raises(ValueError, match="must not be empty"):
        keys_module.normalize_transfer_keys({"id": "  "})
    with pytest.raises(ValueError, match="entries must be strings"):
        keys_module.normalize_transfer_keys([1])
    with pytest.raises(ValueError, match="positive integer"):
        keys_module.normalize_transfer_concurrency(True)
    with pytest.raises(ValueError, match="Multiple transfer_keys"):
        keys_module.normalize_transfer_key_values(
            [keys_module.TransferKey("a", "a"), keys_module.TransferKey("b", "b")],
            [1],
        )
    with pytest.raises(ValueError, match="non-empty sequence"):
        keys_module.normalize_transfer_key_values([keys_module.TransferKey("id", "id")], "one")
    with pytest.raises(ValueError, match="counts must match"):
        keys_module.build_transfer_slice_predicate([keys_module.TransferKey("id", "id")], ())
    assert (
        keys_module.build_transfer_slice_predicate(
            [keys_module.TransferKey("id", "coalesce(id, 0)")], (None,)
        )
        == "(coalesce(id, 0)) IS NULL"
    )
    assert keys_module.render_transfer_literal("O'Reilly") == "'O''Reilly'"
    assert keys_module.render_transfer_literal(Decimal("1.25")) == "1.25"
    with pytest.raises(ValueError, match="Decimal values must be finite"):
        keys_module.render_transfer_literal(Decimal("NaN"))
    with pytest.raises(ValueError, match="supports only"):
        keys_module.render_transfer_literal(object())
