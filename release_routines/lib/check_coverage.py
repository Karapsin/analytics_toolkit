from __future__ import annotations

import argparse
import json
import math
import os
import stat
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, NoReturn, Sequence


class CoverageGateError(ValueError):
    """Raised when coverage inputs cannot be evaluated safely."""


def _fail(message: str, *, cause: Exception | None = None) -> NoReturn:
    error = CoverageGateError(message)
    if cause is not None:
        raise error from cause
    raise error


@dataclass(frozen=True)
class OpportunityCounts:
    covered_statements: int = 0
    statements: int = 0
    covered_branches: int = 0
    branches: int = 0

    @property
    def covered_combined(self) -> int:
        return self.covered_statements + self.covered_branches

    @property
    def combined(self) -> int:
        return self.statements + self.branches

    def percentage(self, metric: str) -> float:
        covered, total = self.opportunities(metric)
        return 100.0 if total == 0 else covered * 100.0 / total

    def floored_percentage(self, metric: str) -> float:
        """Return a deterministic percentage rounded down to two decimals."""
        covered, total = self.opportunities(metric)
        if total == 0:
            return 100.0
        return (covered * 10_000 // total) / 100

    def opportunities(self, metric: str) -> tuple[int, int]:
        if metric == "statements":
            return self.covered_statements, self.statements
        if metric == "branches":
            return self.covered_branches, self.branches
        if metric == "combined":
            return self.covered_combined, self.combined
        _fail(f"Unknown coverage metric: {metric!r}")

    def __add__(self, other: OpportunityCounts) -> OpportunityCounts:
        return OpportunityCounts(
            self.covered_statements + other.covered_statements,
            self.statements + other.statements,
            self.covered_branches + other.covered_branches,
            self.branches + other.branches,
        )


def _nonnegative_int(summary: Mapping[str, Any], key: str, filename: str) -> int:
    value = summary.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        _fail(f"{filename}: summary.{key} must be a non-negative integer")
    return value


def _counts_for_file(filename: str, payload: Any) -> OpportunityCounts:
    if not isinstance(payload, Mapping) or not isinstance(payload.get("summary"), Mapping):
        _fail(f"{filename}: missing coverage summary")
    summary = payload["summary"]
    statements = _nonnegative_int(summary, "num_statements", filename)
    missing_statements = _nonnegative_int(summary, "missing_lines", filename)
    branches = _nonnegative_int(summary, "num_branches", filename)
    missing_branches = _nonnegative_int(summary, "missing_branches", filename)
    if missing_statements > statements or missing_branches > branches:
        _fail(f"{filename}: missing opportunities exceed totals")
    return OpportunityCounts(
        covered_statements=statements - missing_statements,
        statements=statements,
        covered_branches=branches - missing_branches,
        branches=branches,
    )


def load_report(path: Path) -> dict[str, OpportunityCounts]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _fail(f"Cannot read coverage report {path}: {exc}", cause=exc)
    if not isinstance(payload, Mapping) or not isinstance(payload.get("files"), Mapping):
        _fail("Coverage report must contain a files object")
    if not payload["files"]:
        _fail("Coverage report contains no files")
    return {
        str(filename).replace("\\", "/"): _counts_for_file(str(filename), file_payload)
        for filename, file_payload in payload["files"].items()
    }


def _normalized_prefix(prefix: str) -> str:
    normalized = prefix.strip().strip("/").replace("\\", "/")
    package_prefix = "analytics_toolkit/"
    if normalized.startswith(package_prefix):
        normalized = normalized[len(package_prefix) :]
    if normalized.endswith(".py"):
        normalized = normalized[:-3]
    if not normalized:
        _fail("Coverage prefix must not be empty")
    return normalized


def aggregate_prefix(files: Mapping[str, OpportunityCounts], prefix: str) -> OpportunityCounts:
    normalized = _normalized_prefix(prefix)
    matches = []
    for filename, counts in files.items():
        package_relative = filename
        marker = "analytics_toolkit/"
        marker_index = package_relative.find(marker)
        if marker_index >= 0:
            package_relative = package_relative[marker_index + len(marker) :]
        module_name = (
            package_relative[:-3] if package_relative.endswith(".py") else package_relative
        )
        if module_name == normalized or module_name.startswith(f"{normalized}/"):
            matches.append(counts)
    if not matches:
        _fail(f"Coverage prefix {prefix!r} matched no files")
    total = OpportunityCounts()
    for counts in matches:
        total += counts
    return total


def _targets(payload: Any, label: str) -> dict[str, float]:
    if not isinstance(payload, Mapping):
        _fail(f"{label} targets must be an object")
    required = {"statements", "branches", "combined"}
    unknown = set(payload) - required
    if unknown:
        _fail(f"{label} has unknown coverage metrics: {sorted(unknown)}")
    result: dict[str, float] = {}
    for metric, value in payload.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            _fail(f"{label}.{metric} must be numeric")
        target = float(value)
        if not math.isfinite(target) or not 0 <= target <= 100:  # noqa: PLR2004
            _fail(f"{label}.{metric} must be between 0 and 100")
        result[metric] = target
    return result


def load_targets(path: Path) -> tuple[dict[str, float], dict[str, dict[str, float]]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _fail(f"Cannot read coverage targets {path}: {exc}", cause=exc)
    if not isinstance(payload, Mapping):
        _fail("Coverage targets must be an object")
    overall = _targets(payload.get("overall"), "overall")
    if set(overall) != {"statements", "branches", "combined"}:
        _fail("Overall targets must define statements, branches, and combined")
    prefixes_payload = payload.get("prefixes", {})
    if not isinstance(prefixes_payload, Mapping):
        _fail("prefixes targets must be an object")
    prefixes = {
        str(prefix): _targets(target, f"prefixes.{prefix}")
        for prefix, target in prefixes_payload.items()
    }
    return overall, prefixes


def evaluate(
    files: Mapping[str, OpportunityCounts],
    overall_targets: Mapping[str, float],
    prefix_targets: Mapping[str, Mapping[str, float]],
) -> tuple[list[str], list[str]]:
    overall = OpportunityCounts()
    for counts in files.values():
        overall += counts
    scopes = [("overall", overall, overall_targets)]
    scopes.extend(
        (prefix, aggregate_prefix(files, prefix), targets)
        for prefix, targets in prefix_targets.items()
    )
    lines: list[str] = []
    failures: list[str] = []
    for label, counts, targets in scopes:
        for metric in ("statements", "branches", "combined"):
            if metric not in targets:
                continue
            actual = counts.percentage(metric)
            covered, total = counts.opportunities(metric)
            missing = total - covered
            line = (
                f"{label} {metric}: {actual:.2f}% target={targets[metric]:.2f}% "
                f"covered={covered}/{total} missing={missing}"
            )
            lines.append(line)
            if actual + 1e-12 < targets[metric]:
                failures.append(line)
    return lines, failures


def ratchet_targets(
    path: Path,
    files: Mapping[str, OpportunityCounts],
    overall_targets: Mapping[str, float],
    prefix_targets: Mapping[str, Mapping[str, float]],
) -> list[str]:
    """Raise existing floors to measured values without ever lowering them."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _fail(f"Cannot read coverage targets {path}: {exc}", cause=exc)

    overall = OpportunityCounts()
    for counts in files.values():
        overall += counts
    scopes = [("overall", overall, overall_targets)]
    scopes.extend(
        (prefix, aggregate_prefix(files, prefix), targets)
        for prefix, targets in prefix_targets.items()
    )

    changes: list[str] = []
    for label, counts, targets in scopes:
        target_payload = payload["overall"] if label == "overall" else payload["prefixes"][label]
        for metric, current in targets.items():
            measured_floor = counts.floored_percentage(metric)
            if measured_floor <= current:
                continue
            target_payload[metric] = measured_floor
            covered, total = counts.opportunities(metric)
            changes.append(
                f"{label} {metric}: {current:.2f}% -> {measured_floor:.2f}% "
                f"covered={covered}/{total} missing={total - covered} prefix={label}"
            )

    if changes:
        _write_targets_atomically(path, payload)
    return changes


def _write_targets_atomically(path: Path, payload: Any) -> None:
    """Replace a target file atomically with deterministic JSON formatting."""
    temporary_path: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), stat.S_IMODE(path.stat().st_mode))
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary_path.replace(path)
        temporary_path = None
    except OSError as exc:
        _fail(f"Cannot update coverage targets {path}: {exc}", cause=exc)
    finally:
        if temporary_path is not None:
            with suppress(FileNotFoundError):
                temporary_path.unlink()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Enforce exact coverage opportunity floors.")
    parser.add_argument("report", nargs="?", default="coverage.json", type=Path)
    parser.add_argument(
        "--targets",
        default=Path("release_routines/coverage_targets.json"),
        type=Path,
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--update-targets",
        action="store_true",
        help="raise existing floors to measured values rounded down to two decimals",
    )
    mode.add_argument(
        "--update-targets-managed",
        action="store_true",
        help="raise floors and continue after reporting the exact managed changes",
    )
    mode.add_argument(
        "--check-only",
        action="store_true",
        help="validate configured floors without changing the target file",
    )
    args = parser.parse_args(argv)
    try:
        files = load_report(args.report)
        overall, prefixes = load_targets(args.targets)
        lines, failures = evaluate(files, overall, prefixes)
    except CoverageGateError as exc:
        parser.exit(2, f"coverage gate input error: {exc}\n")
    print("\n".join(lines))
    if failures:
        print("Coverage floors missed:\n" + "\n".join(failures))
        return 1
    if args.update_targets or args.update_targets_managed:
        try:
            changes = ratchet_targets(args.targets, files, overall, prefixes)
        except CoverageGateError as exc:
            parser.exit(2, f"coverage gate input error: {exc}\n")
        if changes:
            if args.update_targets_managed:
                print("Coverage targets raised; managed update accepted:\n" + "\n".join(changes))
            else:
                print("Coverage targets raised; review and rerun:\n" + "\n".join(changes))
                return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
