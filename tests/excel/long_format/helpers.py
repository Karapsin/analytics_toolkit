from __future__ import annotations

from tests.excel._support.long_format import (
    Path,
    break_table,
    pd,
    pytest,
)


@pytest.mark.parametrize("invalid_df", [None, "table", 42, [], [pd.DataFrame(), "table"]])
def test_table_helpers_reject_invalid_dataframe_specs(
    invalid_df: object,
    tmp_path: Path,
) -> None:
    expected_error = ValueError if invalid_df == [] else TypeError

    with pytest.raises(expected_error, match="dataframe"):
        break_table(invalid_df, tmp_path / "invalid.xlsx")  # type: ignore[arg-type]
