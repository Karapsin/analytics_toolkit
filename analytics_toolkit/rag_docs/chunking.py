from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from .models import DocChunk
from .text import normalize_identifier


DEFAULT_MAX_CHARS = 2400
DEFAULT_OVERLAP_CHARS = 250
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
SIGNATURE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*\(", flags=re.MULTILINE)


@dataclass(frozen=True)
class _Heading:
    line_no: int
    level: int
    title: str


@dataclass(frozen=True)
class _Section:
    heading_path: tuple[str, ...]
    line_start: int
    line_end: int
    lines: tuple[str, ...]


def chunk_markdown_file(
    path: str | Path,
    root: str | Path = ".",
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    overlap_chars: int = DEFAULT_OVERLAP_CHARS,
) -> list[DocChunk]:
    """Split one Markdown file into heading-aware retrieval chunks."""

    source_path = Path(path).resolve()
    root_path = Path(root).resolve()
    rel_path = source_path.relative_to(root_path).as_posix()
    text = source_path.read_text(encoding="utf-8")
    lines = text.splitlines()
    sections = _split_sections(lines, _extract_headings(text))
    chunks: list[DocChunk] = []
    module = _module_from_path(rel_path)
    function_name = _function_name_from_path(rel_path, text)
    is_function_doc = function_name is not None

    for section in sections:
        for part_index, part in enumerate(
            _split_text("\n".join(section.lines).strip(), max_chars, overlap_chars)
        ):
            if not part.strip():
                continue
            chunk_id = _chunk_id(rel_path, section.line_start, part_index, part)
            chunks.append(
                DocChunk(
                    id=chunk_id,
                    path=rel_path,
                    heading_path=section.heading_path,
                    line_start=section.line_start,
                    line_end=section.line_end,
                    text=part.strip(),
                    module=module,
                    function_name=function_name,
                    is_function_doc=is_function_doc,
                )
            )
    return chunks


def _extract_headings(text: str) -> list[_Heading]:
    markdown_headings = _extract_headings_with_markdown_it(text)
    if markdown_headings is not None:
        return markdown_headings
    return _extract_headings_with_regex(text)


def _extract_headings_with_markdown_it(text: str) -> list[_Heading] | None:
    try:
        from markdown_it import MarkdownIt
    except ImportError:
        return None

    tokens = MarkdownIt("commonmark").parse(text)
    headings: list[_Heading] = []
    for index, token in enumerate(tokens):
        if token.type != "heading_open" or token.map is None:
            continue
        if index + 1 >= len(tokens):
            continue
        inline = tokens[index + 1]
        if inline.type != "inline":
            continue
        try:
            level = int(token.tag[1:] if token.tag.startswith("h") else token.tag)
        except ValueError:
            continue
        headings.append(
            _Heading(
                line_no=int(token.map[0]) + 1,
                level=level,
                title=inline.content.strip(),
            )
        )
    return headings


def _extract_headings_with_regex(text: str) -> list[_Heading]:
    headings: list[_Heading] = []
    in_fence = False
    fence_marker = ""
    for line_no, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            marker = stripped[:3]
            if in_fence and marker == fence_marker:
                in_fence = False
                fence_marker = ""
            elif not in_fence:
                in_fence = True
                fence_marker = marker
            continue
        if in_fence:
            continue
        match = HEADING_RE.match(line)
        if match is None:
            continue
        headings.append(
            _Heading(
                line_no=line_no,
                level=len(match.group(1)),
                title=match.group(2).strip(),
            )
        )
    return headings


def _split_sections(lines: list[str], headings: list[_Heading]) -> list[_Section]:
    if not lines:
        return []

    heading_by_line = {heading.line_no: heading for heading in headings}
    sections: list[_Section] = []
    current_lines: list[str] = []
    current_start = 1
    current_heading_path: tuple[str, ...] = ()
    heading_stack: list[str] = []

    for line_no, line in enumerate(lines, start=1):
        heading = heading_by_line.get(line_no)
        if heading is not None:
            _append_section(
                sections,
                current_heading_path,
                current_start,
                line_no - 1,
                current_lines,
            )
            heading_stack = heading_stack[: heading.level - 1]
            heading_stack.append(heading.title)
            current_heading_path = tuple(heading_stack)
            current_start = line_no
            current_lines = [line]
        else:
            current_lines.append(line)

    _append_section(
        sections,
        current_heading_path,
        current_start,
        len(lines),
        current_lines,
    )
    return sections


def _append_section(
    sections: list[_Section],
    heading_path: tuple[str, ...],
    line_start: int,
    line_end: int,
    lines: list[str],
) -> None:
    text = "\n".join(lines).strip()
    if not text:
        return
    sections.append(
        _Section(
            heading_path=heading_path,
            line_start=line_start,
            line_end=max(line_start, line_end),
            lines=tuple(lines),
        )
    )


def _split_text(text: str, max_chars: int, overlap_chars: int) -> list[str]:
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    if overlap_chars < 0:
        raise ValueError("overlap_chars must be non-negative")
    if len(text) <= max_chars:
        return [text]

    paragraphs = re.split(r"\n\s*\n", text)
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if len(paragraph) > max_chars:
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(_split_long_text(paragraph, max_chars, overlap_chars))
            continue
        candidate = paragraph if not current else f"{current}\n\n{paragraph}"
        if len(candidate) <= max_chars:
            current = candidate
        else:
            chunks.append(current)
            current = _overlap_tail(current, overlap_chars, paragraph)
    if current:
        chunks.append(current)
    return chunks


def _split_long_text(text: str, max_chars: int, overlap_chars: int) -> list[str]:
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + max_chars)
        chunks.append(text[start:end].strip())
        if end == len(text):
            break
        start = max(0, end - overlap_chars)
    return [chunk for chunk in chunks if chunk]


def _overlap_tail(previous: str, overlap_chars: int, next_text: str) -> str:
    if overlap_chars == 0:
        return next_text
    tail = previous[-overlap_chars:].strip()
    return f"{tail}\n\n{next_text}" if tail else next_text


def _module_from_path(rel_path: str) -> str | None:
    parts = rel_path.split("/")
    if len(parts) >= 3 and parts[0] == "docs" and parts[1] == "modules":
        return parts[2]
    return None


def _function_name_from_path(rel_path: str, text: str) -> str | None:
    parts = rel_path.split("/")
    if len(parts) < 5 or parts[0] != "docs" or parts[1] != "modules":
        return None
    if parts[3] != "functions" or parts[-1] == "index.md":
        return None
    match = SIGNATURE_RE.search(_strip_code_fence_language_lines(text))
    if match is not None:
        return match.group(1)
    return normalize_identifier(Path(parts[-1]).stem.replace("-", "_"))


def _strip_code_fence_language_lines(text: str) -> str:
    return "\n".join(
        line
        for line in text.splitlines()
        if not line.strip().startswith(("```", "~~~"))
    )


def _chunk_id(path: str, line_start: int, part_index: int, text: str) -> str:
    value = f"{path}:{line_start}:{part_index}:{text[:120]}".encode("utf-8")
    return hashlib.sha1(value).hexdigest()[:20]
