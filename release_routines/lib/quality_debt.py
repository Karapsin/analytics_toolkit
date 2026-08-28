from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from typing import Any, NoReturn

    FindingCounts = dict[str, dict[str, int]]


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BASELINE = PROJECT_ROOT / "release_routines" / "baselines" / "quality_debt.json"
RUFF_TARGETS = ("analytics_toolkit", "atk", "tests", "agent_tools", "release_routines")
MYPY_TARGETS = ("analytics_toolkit", "atk")
SCHEMA_VERSION = 1


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class DebtIncrease:
    path: str
    rule: str
    baseline_count: int
    current_count: int

    @property
    def added_count(self) -> int:
        return self.current_count - self.baseline_count


@dataclass(frozen=True)
class DebtComparison:
    increases: tuple[DebtIncrease, ...]
    baseline_total: int
    current_total: int
    resolved_total: int
    current_buckets: int


class QualityDebtError(RuntimeError):
    pass


class CommandRunner(Protocol):
    def __call__(self, command: Sequence[str], cwd: Path) -> CommandResult: ...


def _raise_quality_error(
    message: str,
    cause: BaseException | None = None,
) -> NoReturn:
    if cause is None:
        raise QualityDebtError(message)
    raise QualityDebtError(message) from cause


def _run_command(command: Sequence[str], cwd: Path) -> CommandResult:
    try:
        completed = subprocess.run(
            list(command),
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        message = f"Could not run {command[0]!r}: {exc}"
        _raise_quality_error(message, exc)
    return CommandResult(
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def _normalize_path(raw_path: str, root: Path) -> str:
    path = Path(raw_path)
    absolute_path = path if path.is_absolute() else root / path
    try:
        return absolute_path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return absolute_path.resolve().as_posix()


def _increment(
    findings: FindingCounts,
    *,
    path: str,
    rule: str,
) -> None:
    rules = findings.setdefault(path, {})
    rules[rule] = rules.get(rule, 0) + 1


def _sorted_findings(findings: Mapping[str, Mapping[str, int]]) -> FindingCounts:
    return {
        path: {rule: int(rules[rule]) for rule in sorted(rules)}
        for path, rules in sorted(findings.items())
        if rules
    }


def parse_ruff_json(output: str, root: Path) -> FindingCounts:
    try:
        payload = json.loads(output or "[]")
    except json.JSONDecodeError as exc:
        _raise_quality_error("Ruff returned invalid JSON output.", exc)
    if not isinstance(payload, list):
        _raise_quality_error("Ruff JSON output must be a list of findings.")

    findings: FindingCounts = {}
    for entry in payload:
        if not isinstance(entry, dict):
            _raise_quality_error("Ruff JSON findings must be objects.")
        raw_path = entry.get("filename")
        raw_rule = entry.get("code")
        if not isinstance(raw_path, str) or not isinstance(raw_rule, str):
            _raise_quality_error("Ruff findings must include filename and code strings.")
        _increment(
            findings,
            path=_normalize_path(raw_path, root),
            rule=raw_rule,
        )
    return _sorted_findings(findings)


def parse_ruff_format_output(output: str, root: Path) -> FindingCounts:
    findings: FindingCounts = {}
    prefix = "Would reformat: "
    for line in output.splitlines():
        if line.startswith(prefix):
            _increment(
                findings,
                path=_normalize_path(line[len(prefix) :], root),
                rule="FORMAT",
            )
    return _sorted_findings(findings)


def parse_mypy_json_lines(output: str, root: Path) -> FindingCounts:
    findings: FindingCounts = {}
    for line_number, line in enumerate(output.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as exc:
            message = f"Mypy returned invalid JSON on output line {line_number}."
            _raise_quality_error(message, exc)
        if not isinstance(entry, dict):
            _raise_quality_error("Mypy JSON findings must be objects.")
        if entry.get("severity") != "error":
            continue
        raw_path = entry.get("file")
        raw_rule = entry.get("code")
        if not isinstance(raw_path, str) or not isinstance(raw_rule, str):
            _raise_quality_error("Mypy errors must include file and code strings.")
        _increment(
            findings,
            path=_normalize_path(raw_path, root),
            rule=raw_rule,
        )
    return _sorted_findings(findings)


def _validate_tool_result(result: CommandResult, tool_name: str) -> None:
    if result.returncode in {0, 1}:
        return
    details = (result.stderr or result.stdout).strip()
    message = f"{tool_name} failed to run (exit {result.returncode})."
    if details:
        message = f"{message}\n{details}"
    _raise_quality_error(message)


def collect_ruff_findings(
    root: Path,
    runner: CommandRunner = _run_command,
) -> dict[str, FindingCounts]:
    lint_result = runner(
        ("ruff", "check", "--output-format", "json", *RUFF_TARGETS),
        root,
    )
    _validate_tool_result(lint_result, "Ruff lint")
    lint_findings = parse_ruff_json(lint_result.stdout, root)
    if lint_result.returncode == 1 and not lint_findings:
        _raise_quality_error("Ruff lint reported debt but returned no parseable findings.")

    format_result = runner(
        ("ruff", "format", "--check", *RUFF_TARGETS),
        root,
    )
    _validate_tool_result(format_result, "Ruff format")
    format_findings = parse_ruff_format_output(format_result.stdout, root)
    if format_result.returncode == 1 and not format_findings:
        _raise_quality_error("Ruff format reported debt in an unrecognized format.")
    return {"ruff": lint_findings, "ruff_format": format_findings}


def collect_mypy_findings(
    root: Path,
    runner: CommandRunner = _run_command,
) -> dict[str, FindingCounts]:
    result = runner(
        (sys.executable, "-m", "mypy", "-O", "json", *MYPY_TARGETS),
        root,
    )
    _validate_tool_result(result, "Mypy")
    findings = parse_mypy_json_lines(result.stdout, root)
    if result.returncode == 1 and not findings:
        _raise_quality_error("Mypy reported debt but returned no parseable error findings.")
    return {"mypy": findings}


def compare_debt(
    baseline: Mapping[str, Mapping[str, int]],
    current: Mapping[str, Mapping[str, int]],
) -> DebtComparison:
    increases: list[DebtIncrease] = []
    baseline_total = 0
    current_total = 0
    resolved_total = 0

    all_paths = set(baseline) | set(current)
    for path in sorted(all_paths):
        baseline_rules = baseline.get(path, {})
        current_rules = current.get(path, {})
        for rule in sorted(set(baseline_rules) | set(current_rules)):
            baseline_count = int(baseline_rules.get(rule, 0))
            current_count = int(current_rules.get(rule, 0))
            baseline_total += baseline_count
            current_total += current_count
            if current_count > baseline_count:
                increases.append(
                    DebtIncrease(
                        path=path,
                        rule=rule,
                        baseline_count=baseline_count,
                        current_count=current_count,
                    )
                )
            elif current_count < baseline_count:
                resolved_total += baseline_count - current_count

    return DebtComparison(
        increases=tuple(increases),
        baseline_total=baseline_total,
        current_total=current_total,
        resolved_total=resolved_total,
        current_buckets=sum(len(rules) for rules in current.values()),
    )


def _validate_findings(raw_findings: Any, label: str) -> FindingCounts:
    if not isinstance(raw_findings, dict):
        message = f"Baseline section {label!r} must be an object."
        _raise_quality_error(message)
    findings: FindingCounts = {}
    for path, raw_rules in raw_findings.items():
        if not isinstance(path, str) or not isinstance(raw_rules, dict):
            message = f"Baseline section {label!r} has an invalid path entry."
            _raise_quality_error(message)
        rules: dict[str, int] = {}
        for rule, count in raw_rules.items():
            if not isinstance(rule, str) or count.__class__ is not int or count < 1:
                message = f"Baseline section {label!r} has an invalid rule count."
                _raise_quality_error(message)
            rules[rule] = count
        if rules:
            findings[path] = rules
    return _sorted_findings(findings)


def load_baseline(path: Path) -> dict[str, FindingCounts]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        message = f"Quality debt baseline is missing: {path}"
        _raise_quality_error(message, exc)
    except json.JSONDecodeError as exc:
        message = f"Quality debt baseline is not valid JSON: {path}"
        _raise_quality_error(message, exc)
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        _raise_quality_error("Unsupported quality debt baseline schema.")
    raw_tools = payload.get("tools")
    if not isinstance(raw_tools, dict):
        _raise_quality_error("Quality debt baseline must define a tools object.")
    return {
        tool: _validate_findings(findings, tool)
        for tool, findings in raw_tools.items()
        if isinstance(tool, str)
    }


def write_baseline(
    path: Path,
    updates: Mapping[str, Mapping[str, Mapping[str, int]]],
) -> None:
    existing_tools: dict[str, FindingCounts] = {}
    if path.exists():
        existing_tools = load_baseline(path)
    existing_tools.update({tool: _sorted_findings(findings) for tool, findings in updates.items()})
    payload = {
        "schema_version": SCHEMA_VERSION,
        "targets": {
            "mypy": list(MYPY_TARGETS),
            "ruff": list(RUFF_TARGETS),
        },
        "tools": {tool: existing_tools[tool] for tool in sorted(existing_tools)},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(f"{path.suffix}.tmp")
    temporary_path.write_text(
        f"{json.dumps(payload, indent=2, sort_keys=True)}\n",
        encoding="utf-8",
    )
    temporary_path.replace(path)


def _tool_label(tool: str) -> str:
    return {
        "mypy": "Mypy",
        "ruff": "Ruff lint",
        "ruff_format": "Ruff format",
    }[tool]


def _print_comparison(tool: str, comparison: DebtComparison) -> None:
    label = _tool_label(tool)
    print(
        f"{label}: {comparison.current_total} baseline debt finding(s) remain "
        f"across {comparison.current_buckets} file/rule bucket(s); "
        f"baseline capacity is {comparison.baseline_total}."
    )
    if comparison.resolved_total:
        print(
            f"{label}: {comparison.resolved_total} finding(s) were removed "
            "relative to the committed baseline."
        )
    if not comparison.increases:
        return
    print(f"{label}: new or increased findings:")
    for increase in comparison.increases:
        print(
            f"  {increase.path} [{increase.rule}]: "
            f"{increase.current_count} current, "
            f"{increase.baseline_count} baseline "
            f"(+{increase.added_count})"
        )


def run_gate(
    tool_group: str,
    *,
    root: Path,
    baseline_path: Path,
    write: bool,
    runner: CommandRunner = _run_command,
) -> int:
    current_tools = (
        collect_ruff_findings(root, runner)
        if tool_group == "lint"
        else collect_mypy_findings(root, runner)
    )
    if write:
        write_baseline(baseline_path, current_tools)
        tool_names = ", ".join(_tool_label(tool) for tool in current_tools)
        print(f"Updated {tool_names} debt baseline at {baseline_path}.")
        return 0

    baseline_tools = load_baseline(baseline_path)
    failed = False
    for tool, current in current_tools.items():
        if tool not in baseline_tools:
            message = f"Quality debt baseline does not define {_tool_label(tool)}."
            _raise_quality_error(message)
        comparison = compare_debt(baseline_tools[tool], current)
        _print_comparison(tool, comparison)
        failed = failed or bool(comparison.increases)
    return int(failed)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fail when strict Ruff or mypy debt increases.",
    )
    parser.add_argument("tool", choices=("lint", "type"))
    parser.add_argument(
        "--baseline",
        type=Path,
        default=DEFAULT_BASELINE,
        help="Path to the committed quality debt baseline.",
    )
    parser.add_argument(
        "--write-baseline",
        action="store_true",
        help="Explicitly replace this tool group's baseline with current findings.",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    runner: CommandRunner = _run_command,
) -> int:
    args = _build_parser().parse_args(argv)
    baseline_path = args.baseline if args.baseline.is_absolute() else PROJECT_ROOT / args.baseline
    try:
        return run_gate(
            args.tool,
            root=PROJECT_ROOT,
            baseline_path=baseline_path,
            write=args.write_baseline,
            runner=runner,
        )
    except QualityDebtError as exc:
        print(f"Quality debt gate error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
