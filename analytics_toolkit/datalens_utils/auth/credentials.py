"""Obtain a fresh IAM token from yc without printing or saving credentials."""

from __future__ import annotations

import json
import subprocess
from typing import Any

from analytics_toolkit.datalens_utils.errors import DataLensUtilsError
from analytics_toolkit.datalens_utils.session import current_session as session
from analytics_toolkit.datalens_utils.settings import yc_binary


def extract_credentials() -> Any:
    binary = yc_binary()
    command = [
        str(binary),
        "iam",
        "create-token",
        "--format",
        "json",
        "--profile",
        session().runtime["yc_profile"],
    ]
    try:
        result = subprocess.run(command, capture_output=True, check=False, text=True, timeout=30)  # noqa: S603
    except subprocess.TimeoutExpired:
        message = "Timed out obtaining IAM credentials. Check Yandex Cloud connectivity."
        raise DataLensUtilsError(message) from None
    if result.returncode:
        message = (
            "Could not obtain IAM credentials. On a fresh machine, sign in "
            "once:\n  "
            f"{binary}"
            " init --profile "
            f"{session().runtime['yc_profile']}"
            "\nThen rerun dashboard.py. No IAM token was saved."
        )
        raise DataLensUtilsError(message)
    try:
        credentials = json.loads(result.stdout)
        return credentials["iam_token"]
    except (ValueError, KeyError):
        message = "yc returned an unexpected credential response. No IAM token was saved."
        raise DataLensUtilsError(message) from None
