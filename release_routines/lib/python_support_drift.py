"""Post-merge drift check for the rolling CPython support window.

Three CLI steps keep network access separate from the pure comparison:

* ``released`` fetches stable CPython minors (endoflife.date, then python.org);
* ``probe`` installs core and each extra into disposable environments on the newest
  stable CPython;
* ``evaluate`` compares those facts with ``release_routines/python_support.json``.

Exit status 1 means the policy window is stale and needs a reviewed policy PR.
Exit status 3 means release or package data was unavailable; the policy was not
judged and the run should be retried rather than repaired.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
import venv
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .project_metadata import load_project
from .python_support import (
    EXTRAS,
    expected_minimum,
    load_policy,
    minor,
    requires_python,
    version,
    window_size,
)

STALE = 1
UNAVAILABLE = 3
ENDOFLIFE_URL = "https://endoflife.date/api/v1/products/python/"
PYTHON_ORG_URL = (
    "https://www.python.org/api/v2/downloads/release/?is_published=true&pre_release=false"
)
PROBE_TIMEOUT_SECONDS = 1200
CONTROL_REQUIREMENT = "packaging"
UNAVAILABLE_PATTERNS = (
    "connectionerror",
    "connection reset",
    "connection refused",
    "max retries exceeded",
    "name resolution",
    "name or service not known",
    "read timed out",
    "readtimeout",
    "timed out",
    "proxyerror",
    "sslerror",
    "503 service unavailable",
    "502 bad gateway",
    "500 internal server error",
    "504 gateway",
    "429 too many requests",
)


class ReleaseDataUnavailable(RuntimeError):  # noqa: N818 - public outcome name
    """External release or package data could not be obtained reliably."""


@dataclass
class DriftReport:
    reasons: list[str] = field(default_factory=list)
    expected: dict[str, Any] | None = None

    @property
    def stale(self) -> bool:
        return bool(self.reasons)

    def message(self, policy: dict[str, Any]) -> str:
        if not self.stale:
            return (
                f"Python support window is current: {policy['minimum']}-{policy['newest']} "
                f"(lagging extras: {policy['lagging_extras'] or 'none'})."
            )
        lines = ["STALE PYTHON SUPPORT WINDOW: release_routines/python_support.json is outdated."]
        lines.extend(f"- {reason}" for reason in self.reasons)
        if self.expected is None:
            lines.append("- Expected window cannot be computed until core dependencies resolve.")
        else:
            lines.append(
                "- Expected policy: newest={newest}, minimum={minimum}, lagging_extras={lagging} "
                "({window}-minor window, requires-python {requires}).".format(
                    newest=self.expected["newest"],
                    minimum=self.expected["minimum"],
                    lagging=self.expected["lagging_extras"],
                    window=self.expected["window"],
                    requires=self.expected["requires_python"],
                )
            )
        lines.append(
            "Fix with a reviewed PR: update the policy, run "
            "`python -m release_routines.lib.python_support sync`, align tox.ini and "
            "constraints profiles, and update docs/PYTHON_SUPPORT.md."
        )
        return "\n".join(lines)


def _stable_minor_set(found: set[int], source: str) -> list[str]:
    if not found:
        message = f"{source} returned no stable CPython 3 releases"
        raise ReleaseDataUnavailable(message)
    return [version(value) for value in sorted(found)]


def parse_endoflife(payload: Any, today: date) -> list[str]:
    """Stable minors from the endoflife.date v1 product response."""
    try:
        releases = payload["result"]["releases"]
        found = set()
        for release in releases:
            name, latest = release["name"], (release.get("latest") or {}).get("name", "")
            released = release.get("releaseDate")
            if (
                re.fullmatch(r"3\.[0-9]+", str(name))
                and re.fullmatch(r"3\.[0-9]+\.[0-9]+", str(latest))
                and released
                and date.fromisoformat(str(released)) <= today
            ):
                found.add(minor(str(name)))
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        message = f"endoflife.date returned malformed release data: {exc!r}"
        raise ReleaseDataUnavailable(message) from exc
    return _stable_minor_set(found, "endoflife.date")


def parse_python_org(payload: Any) -> list[str]:
    """Stable minors from the python.org downloads API."""
    if not isinstance(payload, list):
        message = "python.org returned malformed release data"
        raise ReleaseDataUnavailable(message)
    found = set()
    for release in payload:
        match = re.fullmatch(r"Python 3\.([0-9]+)\.[0-9]+", str(release.get("name", "")))
        if match and release.get("is_published") is True and release.get("pre_release") is False:
            found.add(int(match.group(1)))
    return _stable_minor_set(found, "python.org")


def _fetch_json(url: str) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": "analytics-toolkit-ci"})  # noqa: S310
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
        return json.load(response)


def _try_source(
    url: str,
    parse: Callable[[Any], list[str]],
    fetch: Callable[[str], Any],
) -> tuple[list[str] | None, str]:
    try:
        return parse(fetch(url)), ""
    except Exception as exc:  # noqa: BLE001 - every source failure is reported together
        return None, f"{url}: {type(exc).__name__}: {exc}"


def fetch_released_minors(
    today: date,
    fetch: Callable[[str], Any] = _fetch_json,
) -> list[str]:
    """Return stable CPython minors, trying each source before declaring unavailability."""
    errors = []
    sources: tuple[tuple[str, Callable[[Any], list[str]]], ...] = (
        (ENDOFLIFE_URL, lambda payload: parse_endoflife(payload, today)),
        (PYTHON_ORG_URL, parse_python_org),
    )
    for url, parse in sources:
        minors, error = _try_source(url, parse, fetch)
        if minors is not None:
            return minors
        errors.append(error)
    message = "CPython release data unavailable from every source: " + "; ".join(errors)
    raise ReleaseDataUnavailable(message)


def classify_probe(returncode: int, output: str) -> str:
    """Classify one pip installation as supported, unsupported, or unavailable."""
    if returncode == 0:
        return "supported"
    lowered = output.lower()
    if any(pattern in lowered for pattern in UNAVAILABLE_PATTERNS):
        return "unavailable"
    return "unsupported"


def requirement_sets(root: Path) -> dict[str, list[str]]:
    project = load_project(root / "pyproject.toml")
    core = [str(item) for item in project["dependencies"]]
    extras = project.get("optional-dependencies", {})
    return {
        "core": core,
        **{extra: core + [str(item) for item in extras[extra]] for extra in EXTRAS},
    }


Runner = Callable[[list[str]], "tuple[int, str]"]


def _environment_python(directory: Path) -> Path:
    return directory / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _run_pip(requirements: list[str]) -> tuple[int, str]:
    """Install ``requirements`` into a disposable environment, building any sdists.

    A full install (not ``--dry-run``) is the support criterion: an extra counts
    only when every distribution, including native ones built from source,
    installs on the running interpreter.
    """
    with tempfile.TemporaryDirectory(prefix="atk-python-support-") as temporary:
        directory = Path(temporary) / "env"
        try:
            venv.EnvBuilder(with_pip=True).create(directory)
        except (OSError, subprocess.CalledProcessError) as exc:
            message = f"Could not create a disposable probe environment: {exc}"
            raise ReleaseDataUnavailable(message) from exc
        command = [
            str(_environment_python(directory)),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-input",
            "--quiet",
            *requirements,
        ]
        try:
            completed = subprocess.run(
                command, capture_output=True, text=True, check=False, timeout=PROBE_TIMEOUT_SECONDS
            )
        except subprocess.TimeoutExpired as exc:
            return 124, f"pip installation timed out after {exc.timeout} seconds"
    return completed.returncode, completed.stdout + completed.stderr


def probe_support(
    python: str,
    requirements: dict[str, list[str]],
    run: Runner = _run_pip,
) -> dict[str, Any]:
    """Resolve core and each extra on the running interpreter."""
    returncode, output = run([CONTROL_REQUIREMENT])
    if returncode != 0:
        message = f"Package index control resolution failed on {python}: {output.strip()[-800:]}"
        raise ReleaseDataUnavailable(message)
    results: dict[str, bool] = {}
    for name, items in requirements.items():
        returncode, output = run(items)
        outcome = classify_probe(returncode, output)
        if outcome == "unavailable":
            message = (
                f"Package data unavailable while resolving {name} on {python}: {output[-800:]}"
            )
            raise ReleaseDataUnavailable(message)
        results[name] = outcome == "supported"
        if not results[name]:
            print(f"{name} does not resolve on CPython {python}:\n{output.strip()[-2000:]}")
    return {
        "python": python,
        "core": results.pop("core"),
        "extras": {extra: results[extra] for extra in EXTRAS},
    }


def evaluate_drift(
    policy: dict[str, Any],
    released: list[str],
    probe: dict[str, Any],
) -> DriftReport:
    """Pure comparison of the committed policy with observed releases and extra support."""
    if not released:
        message = "No released CPython minors were supplied"
        raise ReleaseDataUnavailable(message)
    newest = max(released, key=minor)
    if probe.get("python") != newest or set(probe.get("extras", {})) != set(EXTRAS):
        message = f"Extra support probe does not describe newest stable CPython {newest}"
        raise ReleaseDataUnavailable(message)
    report = DriftReport()
    current = policy["newest"]
    if minor(newest) > minor(current):
        report.reasons.append(
            f"CPython {newest} has been released, but the policy window still ends at {current}."
        )
    elif minor(newest) < minor(current):
        report.reasons.append(
            f"The policy window ends at {current}, but the newest stable CPython is {newest}."
        )
    if not probe["core"]:
        report.reasons.append(f"Core dependencies do not resolve on CPython {newest}.")
        return report
    lagging = sorted(extra for extra in EXTRAS if not probe["extras"][extra])
    if newest == current:
        caught_up = sorted(set(policy["lagging_extras"]) - set(lagging))
        regressed = sorted(set(lagging) - set(policy["lagging_extras"]))
        if caught_up:
            action = "shrink the window back to seven minors" if not lagging else "update lagging"
            report.reasons.append(
                f"Lagging extras now install on CPython {newest}: {', '.join(caught_up)}; {action}."
            )
        if regressed:
            report.reasons.append(
                f"Extras no longer install on CPython {newest}: {', '.join(regressed)}; "
                "extend the window to eight minors until they support it."
            )
    report.expected = {
        "newest": newest,
        "minimum": expected_minimum(newest, lagging),
        "lagging_extras": lagging,
        "window": window_size(lagging),
        "requires_python": requires_python(
            {"minimum": expected_minimum(newest, lagging), "newest": newest}
        ),
    }
    return report


def _write_output(name: str, value: str) -> None:
    target = os.environ.get("GITHUB_OUTPUT")
    if target:
        with Path(target).open("a", encoding="utf-8") as handle:
            handle.write(f"{name}={value}\n")


def _unavailable(exc: ReleaseDataUnavailable) -> int:
    print(f"::error title=Python release data unavailable::{exc}", file=sys.stderr)
    print(
        "RELEASE DATA UNAVAILABLE: the support window was not evaluated; rerun the workflow. "
        "This is not a stale-policy result.",
        file=sys.stderr,
    )
    return UNAVAILABLE


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("released", "probe", "evaluate"))
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path)
    parser.add_argument("--released", type=Path)
    parser.add_argument("--probe", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.action == "released":
            minors = fetch_released_minors(datetime.now(timezone.utc).date())
            args.output.write_text(json.dumps({"minors": minors}), encoding="utf-8")
            _write_output("newest", minors[-1])
            print(f"Stable CPython minors: {', '.join(minors)}")
            return 0
        if args.action == "probe":
            python = f"{sys.version_info.major}.{sys.version_info.minor}"
            result = probe_support(python, requirement_sets(args.root))
            args.output.write_text(json.dumps(result), encoding="utf-8")
            print(json.dumps(result))
            return 0
        policy = load_policy(args.root)
        released = json.loads(args.released.read_text(encoding="utf-8"))["minors"]
        probe = json.loads(args.probe.read_text(encoding="utf-8"))
        report = evaluate_drift(policy, released, probe)
    except ReleaseDataUnavailable as exc:
        return _unavailable(exc)
    print(report.message(policy))
    if report.stale:
        print(
            "::error title=Stale Python support window::"
            + report.message(policy).replace("\n", "%0A"),
            file=sys.stderr,
        )
        return STALE
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
