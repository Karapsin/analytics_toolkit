"""Byte-for-byte legacy contracts; never normalize exported SDK fields away."""

import os
import subprocess
import sys

import pytest

from tests._support.paths import REPO_ROOT


@pytest.mark.parametrize("hash_seed", ["0", "1"])
def test_financial_exports_equal_baseline(tmp_path, hash_seed):
    expected = REPO_ROOT / "tests/datalens_utils/_support/fixtures/financial_exports"
    destination = tmp_path / "exports"
    subprocess.run(
        [sys.executable, "-m", "tests.datalens_utils._support.financial_exports", str(destination)],
        cwd=REPO_ROOT,
        env={**os.environ, "PYTHONHASHSEED": hash_seed},
        check=True,
        capture_output=True,
    )
    actual = {path.name: path.read_bytes() for path in destination.iterdir()}
    assert set(actual) == {path.name for path in expected.iterdir()}
    for name, content in actual.items():
        assert content == (expected / name).read_bytes(), name
