from __future__ import annotations

import ast
import importlib
import inspect
import pkgutil
from pathlib import Path

import pytest

from tests._support.paths import REPO_ROOT

PROJECT_ROOT = REPO_ROOT

SQL_ROOT = PROJECT_ROOT / "analytics_toolkit" / "sql"

__all__ = [
    "PROJECT_ROOT",
    "REPO_ROOT",
    "SQL_ROOT",
    "Path",
    "ast",
    "importlib",
    "inspect",
    "pkgutil",
    "pytest",
]
