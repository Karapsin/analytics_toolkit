"""Byte-for-byte legacy contracts; never normalize exported SDK fields away."""

import os
import subprocess
import sys

from tests._support.paths import REPO_ROOT


def test_financial_exports_equal_baseline(tmp_path):
    expected = REPO_ROOT / "tests/datalens_utils/_support/fixtures/financial_exports"
    destination = tmp_path / "exports"
    subprocess.run(
        [sys.executable, "-m", "tests.datalens_utils._support.financial_exports", str(destination)],
        cwd=REPO_ROOT,
        env={**os.environ, "PYTHONHASHSEED": "0"},
        check=True,
        capture_output=True,
    )
    actual = {path.name: path.read_bytes() for path in destination.iterdir()}
    assert set(actual) == {path.name for path in expected.iterdir()}
    for name, content in actual.items():
        assert content == (expected / name).read_bytes(), name
