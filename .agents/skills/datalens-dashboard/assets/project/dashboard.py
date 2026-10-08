"""Run a dashboard recipe with the already installed analytics toolkit."""

import os
import sys
from pathlib import Path

from analytics_toolkit.datalens_utils import DataLensProject, Deployment
from analytics_toolkit.datalens_utils.bootstrap import default_runtime_root
from analytics_toolkit.datalens_utils.cli import run_cli

ROOT = Path(__file__).resolve().parent
RUNTIME_ROOT = default_runtime_root(ROOT)
ORG_ID = @ORGANIZATION_ID@
YC_PROFILE = @YC_PROFILE@
TARGET_PATH = @TARGET_PATH@
DASHBOARD_NAME = @DASHBOARD_NAME@
CONNECTION_NAME = @CONNECTION_NAME@
CONNECTION_ID = @CONNECTION_ID@


def yc_binary():
    return os.environ.get("DATALENS_YC_BIN", "yc")


def make_project(*, client_factory=None, reporter=None):
    deployment = Deployment(
        organization_id=ORG_ID, yc_profile=YC_PROFILE, target_path=TARGET_PATH,
        dashboard_name=DASHBOARD_NAME, connection_name=CONNECTION_NAME,
        connection_id=CONNECTION_ID, yc_binary=yc_binary(),
    )
    return DataLensProject(ROOT, RUNTIME_ROOT, deployment,
                           client_factory=client_factory, reporter=reporter)


def main(argv=()):
    return run_cli(make_project(), argv)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
