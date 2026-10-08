"""The all extra stays equal to the union of optional module dependencies."""

from release_routines.lib.project_metadata import load_project

from tests._support.paths import REPO_ROOT


def test_all_extra_installs_every_optional_module():
    project = load_project(REPO_ROOT / "pyproject.toml")
    extras = project["optional-dependencies"]
    assert "all" in extras
    expected = {
        requirement
        for extra, requirements in extras.items()
        if extra != "all"
        for requirement in requirements
    }
    assert set(extras["all"]) == expected
    assert len(extras["all"]) == len(expected)
    assert extras["datalens"] == ["datalens-sdk==3.1.0; python_version >= '3.10'"]
