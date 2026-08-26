from __future__ import annotations

import os
import re
import stat
import tempfile
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

from .config_path import find_connections_file_path
from .errors import SqlConfigError

SECRETS_FILE_NAME = ".secrets"
_SECRET_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_ASSIGNMENT_RE = re.compile(r"(?:export[ \t]+)?(?P<name>[A-Za-z_][A-Za-z0-9_]*)=(?P<value>.*)\Z")


@dataclass(frozen=True)
class _SecretFile:
    path: Path
    existed: bool
    original_text: str
    lines: list[str]
    values: dict[str, str]
    line_indexes: dict[str, int]
    mode: int | None


def load_secret_values() -> tuple[Path, dict[str, str]]:
    secret_file = _read_secret_file(required=True)
    return secret_file.path, secret_file.values


def _read_secret_file(*, required: bool) -> _SecretFile:
    path = _get_secret_file_path()
    try:
        original_text = path.read_text(encoding="utf-8")
        file_stat = path.stat()
    except FileNotFoundError as exc:
        if required:
            message = f"Missing SQL secrets file: {path}"
            raise SqlConfigError(message) from exc
        return _SecretFile(
            path=path,
            existed=False,
            original_text="",
            lines=[],
            values={},
            line_indexes={},
            mode=None,
        )
    except UnicodeDecodeError as exc:
        message = f"SQL secrets file must contain UTF-8 text: {path}"
        raise SqlConfigError(message) from exc

    mode = stat.S_IMODE(file_stat.st_mode) if os.name == "posix" else None
    if mode is not None and mode & 0o077:
        warnings.warn(
            f"SQL secrets file permissions are {mode:04o}; use 0600: {path}",
            UserWarning,
            stacklevel=3,
        )
    lines = original_text.splitlines()
    values, line_indexes = _parse_secret_lines(lines, path)
    return _SecretFile(
        path=path,
        existed=True,
        original_text=original_text,
        lines=lines,
        values=values,
        line_indexes=line_indexes,
        mode=mode,
    )


def _get_secret_file_path() -> Path:
    connections_path = find_connections_file_path()
    if connections_path is None:
        message = "Missing SQL connections file: .connections."
        raise SqlConfigError(message)
    return connections_path.with_name(SECRETS_FILE_NAME)


def _parse_secret_lines(
    lines: list[str],
    path: Path,
) -> tuple[dict[str, str], dict[str, int]]:
    values: dict[str, str] = {}
    line_indexes: dict[str, int] = {}
    for line_index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _ASSIGNMENT_RE.fullmatch(stripped)
        if match is None:
            _raise_invalid_assignment(path, line_index)
        name = match.group("name")
        if name in values:
            message = (
                f"SQL secrets file contains duplicate key '{name}' at line {line_index + 1}: {path}"
            )
            raise SqlConfigError(message)
        values[name] = _parse_secret_value(
            match.group("value"),
            path,
            line_index,
        )
        line_indexes[name] = line_index
    return values, line_indexes


def _parse_secret_value(raw_value: str, path: Path, line_index: int) -> str:
    if not raw_value:
        _raise_invalid_assignment(path, line_index)

    parts: list[str] = []
    cursor = 0
    while cursor < len(raw_value):
        if raw_value[cursor] == "'":
            closing_quote = raw_value.find("'", cursor + 1)
            if closing_quote == -1:
                _raise_invalid_assignment(path, line_index)
            parts.append(raw_value[cursor + 1 : closing_quote])
            cursor = closing_quote + 1
        elif raw_value.startswith("\\'", cursor):
            parts.append("'")
            cursor += 2
        else:
            _raise_invalid_assignment(path, line_index)
    value = "".join(parts)
    if "\x00" in value:
        _raise_invalid_assignment(path, line_index)
    return value


def _raise_invalid_assignment(path: Path, line_index: int) -> NoReturn:
    message = (
        "SQL secrets file line "
        f"{line_index + 1} must use [export ]NAME='value' with no spaces "
        f"around '=': {path}"
    )
    raise SqlConfigError(message)


def _write_secret_file(
    secret_file: _SecretFile,
    pending_values: dict[str, str],
) -> None:
    _ensure_secret_file_unchanged(secret_file)
    lines = list(secret_file.lines)
    for name, value in pending_values.items():
        assignment = f"export {name}={_quote_secret_value(name, value)}"
        line_index = secret_file.line_indexes.get(name)
        if line_index is None:
            lines.append(assignment)
        else:
            lines[line_index] = assignment

    updated_text = "\n".join(lines) + "\n"
    file_descriptor, raw_temp_path = tempfile.mkstemp(
        prefix=f"{SECRETS_FILE_NAME}.",
        dir=secret_file.path.parent,
        text=True,
    )
    temp_path = Path(raw_temp_path)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as stream:
            if os.name == "posix":  # pragma: no branch - platform-specific
                os.fchmod(
                    stream.fileno(),
                    secret_file.mode if secret_file.mode is not None else 0o600,
                )
            stream.write(updated_text)
            stream.flush()
            os.fsync(stream.fileno())
        temp_path.replace(secret_file.path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


def _ensure_secret_file_unchanged(secret_file: _SecretFile) -> None:
    try:
        current_text = secret_file.path.read_text(encoding="utf-8")
    except FileNotFoundError:
        current_text = None
    if secret_file.existed:
        unchanged = current_text == secret_file.original_text
    else:
        unchanged = current_text is None
    if not unchanged:
        message = f"SQL secrets file changed while values were being entered: {secret_file.path}"
        raise SqlConfigError(message)


def _quote_secret_value(name: str, value: str) -> str:
    if _SECRET_NAME_RE.fullmatch(name) is None:
        message = f"Invalid SQL secret key: {name!r}"
        raise SqlConfigError(message)
    if "\x00" in value or "\n" in value or "\r" in value:
        message = f"SQL secret '{name}' cannot contain NUL or newline characters."
        raise SqlConfigError(message)
    return "'" + value.replace("'", "'\\''") + "'"


__all__: list[str] = []
