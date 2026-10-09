"""Run a dashboard recipe with the already installed analytics toolkit."""

import os
import sys
from pathlib import Path

from analytics_toolkit.datalens_utils import BIProjectDeployment, DataLensProject, Deployment, TargetLocation
from analytics_toolkit.datalens_utils.bootstrap import default_runtime_root
from analytics_toolkit.datalens_utils.cli import run_cli

ROOT = Path(__file__).resolve().parent
RUNTIME_ROOT = default_runtime_root(ROOT)
ORG_ID = "offline"
YC_PROFILE = ""
TARGET_PATH = "Offline/BI"
DASHBOARD_NAME = "BI example"
CONNECTION_NAME = "Existing ClickHouse"
CONNECTION_ID = "example-connection"


def yc_binary():
    return os.environ.get("DATALENS_YC_BIN", "yc")


def make_project(*, client_factory=None, reporter=None):
    deployment = BIProjectDeployment(DASHBOARD_NAME, TargetLocation.path(TARGET_PATH),
        installation='yc', organization_id=ORG_ID, yc_profile=YC_PROFILE,
        yc_binary=yc_binary(), base_url=None, token_env=None)
    return DataLensProject(ROOT, RUNTIME_ROOT, deployment,
                           client_factory=client_factory, reporter=reporter)


def main(argv=()):
    return run_cli(make_project(), argv)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
