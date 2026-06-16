#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence


DEFAULT_INDEX_DIR = ".rag_index"
DEFAULT_MAX_CHARS = 2400
DEFAULT_OVERLAP_CHARS = 250
INDEX_VERSION = 2
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
SIGNATURE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*\(", flags=re.MULTILINE)
TOKEN_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*")
SOURCE_PUBLIC_DOCS = "public_docs"
SOURCE_AGENT_DOCS = "agent_docs"
SOURCE_AGENT_TOOLS = "agent_tools"
AGENT_QUERY_TOKENS = {
    "agent",
    "agent_docs",
    "agent_tools",
    "development",
    "docs_assistant",
    "instruction",
    "instructions",
    "pre_commit",
    "pre_commit_checks",
    "rag",
    "release",
    "retrieval",
}
QUERY_EXPANSIONS = {
    "rag": ("docs_assistant", "retrieval", "index", "search"),
    "precommit": ("pre_commit", "pre_commit_checks"),
    "pre_commit": ("pre_commit_checks",),
    "gp": ("greenplum",),
    "ch": ("clickhouse",),
}
STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "can",
    "do",
    "docs",
    "for",
    "from",
    "how",
    "i",
    "in",
    "is",
    "it",
    "of",
    "on",
    "or",
    "the",
    "to",
    "use",
    "what",
    "when",
    "where",
    "with",
}


@dataclass(frozen=True)
class DocChunk:
    id: str
    path: str
    heading_path: tuple[str, ...]
    line_start: int
    line_end: int
    text: str
    module: str | None = None
    function_name: str | None = None
    is_function_doc: bool = False
    source_type: str = SOURCE_PUBLIC_DOCS

    @property
    def heading(self) -> str:
        return " > ".join(self.heading_path)

    @property
    def citation(self) -> str:
        return f"{self.path}:L{self.line_start}"

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "path": self.path,
            "heading_path": list(self.heading_path),
            "line_start": self.line_start,
            "line_end": self.line_end,
            "text": self.text,
            "module": self.module,
            "function_name": self.function_name,
            "is_function_doc": self.is_function_doc,
            "source_type": self.source_type,
        }

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> "DocChunk":
        heading_path = value.get("heading_path", [])
        if not isinstance(heading_path, list):
            heading_path = []
        return cls(
            id=str(value["id"]),
            path=str(value["path"]),
            heading_path=tuple(str(part) for part in heading_path),
            line_start=int(value["line_start"]),
            line_end=int(value["line_end"]),
            text=str(value["text"]),
            module=_optional_string(value.get("module")),
            function_name=_optional_string(value.get("function_name")),
            is_function_doc=bool(value.get("is_function_doc", False)),
            source_type=str(value.get("source_type") or SOURCE_PUBLIC_DOCS),
        )


@dataclass(frozen=True)
class SearchResult:
    chunk: DocChunk
    score: float
    lexical_score: float = 0.0


@dataclass(frozen=True)
class IndexBuildResult:
    index_dir: Path
    file_count: int
    chunk_count: int


@dataclass(frozen=True)
class AnswerResult:
    answer: str
    results: tuple[SearchResult, ...]

    @property
    def citations(self) -> list[str]:
        return [result.chunk.citation for result in self.results]


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


def discover_markdown_files(root: str | Path = ".") -> list[Path]:
    root_path = Path(root).resolve()
    files: list[Path] = []

    readme = root_path / "README.md"
    if readme.is_file():
        files.append(readme)

    docs_dir = root_path / "docs"
    if docs_dir.is_dir():
        files.extend(sorted(path for path in docs_dir.rglob("*.md") if path.is_file()))

    agent_docs_dir = root_path / "agent_docs"
    if agent_docs_dir.is_dir():
        files.extend(
            sorted(path for path in agent_docs_dir.rglob("*.md") if path.is_file())
        )

    agent_tools_readme = root_path / "agent_tools" / "README.md"
    if agent_tools_readme.is_file():
        files.append(agent_tools_readme)

    return sorted(files, key=lambda path: path.relative_to(root_path).as_posix())


def chunk_markdown_file(
    path: str | Path,
    root: str | Path = ".",
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    overlap_chars: int = DEFAULT_OVERLAP_CHARS,
) -> list[DocChunk]:
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
    source_type = _source_type_from_path(rel_path)

    for section in sections:
        for part_index, part in enumerate(
            _split_text("\n".join(section.lines).strip(), max_chars, overlap_chars)
        ):
            if not part.strip():
                continue
            chunks.append(
                DocChunk(
                    id=_chunk_id(rel_path, section.line_start, part_index, part),
                    path=rel_path,
                    heading_path=section.heading_path,
                    line_start=section.line_start,
                    line_end=section.line_end,
                    text=part.strip(),
                    module=module,
                    function_name=function_name,
                    is_function_doc=is_function_doc,
                    source_type=source_type,
                )
            )
    return chunks


def build_docs_index(
    root: str | Path = ".",
    index_dir: str | Path = DEFAULT_INDEX_DIR,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    overlap_chars: int = DEFAULT_OVERLAP_CHARS,
) -> IndexBuildResult:
    root_path = Path(root).resolve()
    index_path = Path(index_dir)
    if not index_path.is_absolute():
        index_path = root_path / index_path
    index_path.mkdir(parents=True, exist_ok=True)

    files = discover_markdown_files(root_path)
    chunks: list[DocChunk] = []
    for path in files:
        chunks.extend(
            chunk_markdown_file(
                path,
                root_path,
                max_chars=max_chars,
                overlap_chars=overlap_chars,
            )
        )

    _write_json(
        index_path / "chunks.json",
        {"version": INDEX_VERSION, "chunks": [chunk.to_dict() for chunk in chunks]},
    )
    _write_json(
        index_path / "manifest.json",
        {
            "version": INDEX_VERSION,
            "root": str(root_path),
            "file_count": len(files),
            "chunk_count": len(chunks),
            "retrieval": "lexical",
            "tool": "agent_tools/docs_assistant.py",
            "source_files": _source_manifest(root_path, files),
        },
    )
    return IndexBuildResult(index_path, len(files), len(chunks))


def search_docs(
    question: str,
    index_dir: str | Path = DEFAULT_INDEX_DIR,
    *,
    top_k: int = 5,
) -> list[SearchResult]:
    if top_k <= 0:
        raise ValueError("top_k must be positive")
    chunks = load_chunks(index_dir)
    if not chunks:
        return []

    lexical_scores = _lexical_scores(question, chunks)
    combined: list[SearchResult] = []
    for chunk in chunks:
        lexical_score = lexical_scores.get(chunk.id, 0.0)
        score = lexical_score + _metadata_boost(question, chunk)
        if score > 0:
            combined.append(
                SearchResult(
                    chunk=chunk,
                    score=score,
                    lexical_score=lexical_score,
                )
            )

    combined.sort(
        key=lambda result: (
            result.score,
            result.chunk.is_function_doc,
            -result.chunk.line_start,
        ),
        reverse=True,
    )
    return combined[:top_k]


def ask_docs(
    question: str,
    index_dir: str | Path = DEFAULT_INDEX_DIR,
    *,
    top_k: int = 5,
    no_llm: bool = True,
) -> AnswerResult:
    if not no_llm:
        raise ValueError("Only --no-llm mode is supported by the stdlib agent tool.")
    results = tuple(search_docs(question, index_dir, top_k=top_k))
    if not results:
        return AnswerResult(
            answer=(
                "I could not find enough relevant documentation context in the local "
                "agent docs index to answer that question."
            ),
            results=(),
        )
    return AnswerResult(answer=_fallback_answer(results), results=results)


def load_chunks(index_dir: str | Path = DEFAULT_INDEX_DIR) -> list[DocChunk]:
    data = _read_json(Path(index_dir) / "chunks.json")
    chunks = data.get("chunks", [])
    if not isinstance(chunks, list):
        raise ValueError("Invalid docs index: chunks must be a list")
    return [DocChunk.from_dict(chunk) for chunk in chunks if isinstance(chunk, dict)]


def _extract_headings(text: str) -> list[_Heading]:
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

    _append_section(sections, current_heading_path, current_start, len(lines), current_lines)
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
    if overlap_chars >= max_chars:
        raise ValueError("overlap_chars must be smaller than max_chars")
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


def _lexical_scores(question: str, chunks: list[DocChunk]) -> dict[str, float]:
    query_tokens = query_tokens_for_search(question)
    if not query_tokens:
        return {}
    corpus_tokens = [tokenize(format_chunk_for_search(chunk)) for chunk in chunks]
    raw_scores = _fallback_bm25_scores(query_tokens, corpus_tokens)
    normalized = _normalize(raw_scores)
    return {chunk.id: score for chunk, score in zip(chunks, normalized)}


def _fallback_bm25_scores(
    query_tokens: list[str],
    corpus_tokens: list[list[str]],
) -> list[float]:
    if not corpus_tokens:
        return []
    doc_freq: Counter[str] = Counter()
    doc_lengths = [len(tokens) for tokens in corpus_tokens]
    avg_doc_length = sum(doc_lengths) / len(doc_lengths) if doc_lengths else 1.0
    for tokens in corpus_tokens:
        doc_freq.update(set(tokens))

    query_counts = Counter(query_tokens)
    scores: list[float] = []
    total_docs = len(corpus_tokens)
    k1 = 1.5
    b = 0.75
    for tokens in corpus_tokens:
        token_counts = Counter(tokens)
        doc_length = max(1, len(tokens))
        score = 0.0
        for token, query_count in query_counts.items():
            freq = token_counts.get(token, 0)
            if freq == 0:
                continue
            idf = math.log(1 + (total_docs - doc_freq[token] + 0.5) / (doc_freq[token] + 0.5))
            denominator = freq + k1 * (1 - b + b * doc_length / avg_doc_length)
            score += query_count * idf * (freq * (k1 + 1)) / denominator
        scores.append(score)
    return scores


def _metadata_boost(question: str, chunk: DocChunk) -> float:
    query_normalized = normalize_identifier(question)
    query_tokens = set(query_tokens_for_search(question))
    boost = 0.0
    if chunk.function_name:
        function_normalized = normalize_identifier(chunk.function_name)
        if function_normalized and function_normalized in query_normalized:
            boost += 0.45
        elif set(function_normalized.split("_")) & query_tokens:
            boost += 0.12
    if chunk.module and chunk.module in query_tokens:
        boost += 0.08
    path_tokens = set(tokenize(chunk.path))
    heading_tokens = set(tokenize(chunk.heading))
    boost += min(0.12, 0.03 * len(query_tokens & path_tokens))
    boost += min(0.12, 0.03 * len(query_tokens & heading_tokens))
    if query_tokens & AGENT_QUERY_TOKENS:
        if chunk.source_type == SOURCE_AGENT_TOOLS:
            boost += 0.50
        elif chunk.source_type == SOURCE_AGENT_DOCS:
            boost += 0.42
    if chunk.source_type in {SOURCE_AGENT_DOCS, SOURCE_AGENT_TOOLS}:
        source_tokens = set(tokenize(chunk.source_type))
        boost += min(0.08, 0.04 * len(query_tokens & source_tokens))
    if chunk.is_function_doc and query_tokens & {"signature", "input", "inputs", "function"}:
        boost += 0.10
    if _is_sql_domain_query(query_tokens):
        if chunk.module == "sql" or chunk.path.startswith("docs/AIRFLOW_SQL_MANUAL.md"):
            boost += 0.18
        if (
            chunk.path == "docs/modules/sql/configuration.md"
            and query_tokens
            & {"configure", "configuration", "connection", "connections", "alias", "aliases"}
        ):
            boost += 0.75
    return boost


def _is_sql_domain_query(query_tokens: set[str]) -> bool:
    return bool(
        query_tokens
        & {
            "airflow",
            "ch",
            "clickhouse",
            "connection",
            "connections",
            "gp",
            "greenplum",
            "sql",
            "trino",
        }
    )


def format_chunk_for_search(chunk: DocChunk) -> str:
    metadata = [
        f"Path: {chunk.path}",
        f"Heading: {chunk.heading}" if chunk.heading else "",
        f"Source: {chunk.source_type}",
        f"Module: {chunk.module}" if chunk.module else "",
        f"Function: {chunk.function_name}" if chunk.function_name else "",
    ]
    metadata_text = "\n".join(part for part in metadata if part)
    return f"{metadata_text}\n\n{chunk.text}".strip()


def tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    for match in TOKEN_RE.finditer(text.lower()):
        raw = match.group(0).strip("._-")
        if not raw:
            continue
        _append_token(tokens, raw)
        for part in re.split(r"[._-]+", raw):
            _append_token(tokens, part)
    return tokens


def query_tokens_for_search(question: str) -> list[str]:
    tokens = tokenize(question)
    expanded = list(tokens)
    normalized = normalize_identifier(question)
    if "docs_assistant" in normalized or "docs_assistant" in tokens:
        _extend_unique(expanded, ("docs_assistant",))
    for token in tokens:
        _extend_unique(expanded, QUERY_EXPANSIONS.get(token, ()))
    return expanded


def normalize_identifier(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def snippet(text: str, max_chars: int = 260) -> str:
    compact = re.sub(r"\s+", " ", text).strip()
    if len(compact) <= max_chars:
        return compact
    return compact[: max_chars - 3].rstrip() + "..."


def _fallback_answer(results: tuple[SearchResult, ...]) -> str:
    lines = [
        "Most relevant passages:",
    ]
    for index, result in enumerate(results, start=1):
        chunk = result.chunk
        heading = f" - {chunk.heading}" if chunk.heading else ""
        lines.append(
            f"[{index}] {chunk.citation}{heading}: {snippet(chunk.text, 360)}"
        )
    return "\n".join(lines)


def _module_from_path(rel_path: str) -> str | None:
    parts = rel_path.split("/")
    if len(parts) >= 3 and parts[0] == "docs" and parts[1] == "modules":
        return parts[2]
    return None


def _source_type_from_path(rel_path: str) -> str:
    if rel_path.startswith("agent_docs/"):
        return SOURCE_AGENT_DOCS
    if rel_path == "agent_tools/README.md":
        return SOURCE_AGENT_TOOLS
    return SOURCE_PUBLIC_DOCS


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


def _normalize(values: list[float]) -> list[float]:
    max_value = max(values, default=0.0)
    if max_value <= 0:
        return [0.0 for _ in values]
    return [value / max_value for value in values]


def _append_token(tokens: list[str], token: str) -> None:
    if token and token not in STOPWORDS:
        tokens.append(token)


def _extend_unique(tokens: list[str], additions: Sequence[str]) -> None:
    seen = set(tokens)
    for token in additions:
        for expanded_token in tokenize(token):
            if expanded_token not in seen:
                tokens.append(expanded_token)
                seen.add(expanded_token)


def _source_manifest(root_path: Path, files: list[Path]) -> list[dict[str, object]]:
    source_files = []
    for path in files:
        source_files.append(
            {
                "path": path.relative_to(root_path).as_posix(),
                "mtime_ns": path.stat().st_mtime_ns,
            }
        )
    return source_files


def index_freshness_warnings(index_dir: str | Path = DEFAULT_INDEX_DIR) -> list[str]:
    index_path = Path(index_dir)
    manifest = _read_json(index_path / "manifest.json")
    warnings: list[str] = []
    version = int(manifest.get("version", 0))
    if version != INDEX_VERSION:
        warnings.append(
            f"Docs index version is {version}, expected {INDEX_VERSION}; rebuild the index."
        )

    root = Path(str(manifest.get("root") or "."))
    source_files = manifest.get("source_files")
    if not isinstance(source_files, list):
        warnings.append("Docs index has no source file metadata; rebuild the index.")
        return warnings

    indexed_paths: set[str] = set()
    for source_file in source_files:
        if not isinstance(source_file, dict):
            continue
        rel_path = str(source_file.get("path") or "")
        if not rel_path:
            continue
        indexed_paths.add(rel_path)
        indexed_mtime_ns = int(source_file.get("mtime_ns") or 0)
        source_path = root / rel_path
        if not source_path.is_file():
            warnings.append(f"Indexed source file is missing: {rel_path}")
            continue
        if source_path.stat().st_mtime_ns > indexed_mtime_ns:
            warnings.append(f"Docs index is stale: {rel_path} changed after indexing.")
    current_paths = {
        path.relative_to(root).as_posix()
        for path in discover_markdown_files(root)
    }
    for rel_path in sorted(current_paths - indexed_paths):
        warnings.append(f"Docs index is stale: new source file is not indexed: {rel_path}")
    return warnings


def _warn_if_index_stale(index_dir: str | Path) -> None:
    try:
        warnings = index_freshness_warnings(index_dir)
    except FileNotFoundError:
        return
    except ValueError as exc:
        print(f"Docs index freshness check failed: {exc}", file=sys.stderr)
        return
    for warning in warnings:
        print(f"Warning: {warning}", file=sys.stderr)


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"Docs index file not found: {path}")
    with path.open("r", encoding="utf-8") as json_file:
        data = json.load(json_file)
    if not isinstance(data, dict):
        raise ValueError(f"Docs index file must contain an object: {path}")
    return data


def _write_json(path: Path, data: dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8") as json_file:
        json.dump(data, json_file, ensure_ascii=False, indent=2, sort_keys=True)
        json_file.write("\n")


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(Path.cwd()))
    except ValueError:
        return str(path)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "handler"):
        parser.print_help()
        return 2
    return int(args.handler(args))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python agent_tools/docs_assistant.py")
    subparsers = parser.add_subparsers(dest="command")

    index_parser = subparsers.add_parser("index")
    index_parser.add_argument("--root", default=".")
    index_parser.add_argument("--index-dir", default=DEFAULT_INDEX_DIR)
    index_parser.set_defaults(handler=_handle_index)

    search_parser = subparsers.add_parser("search")
    search_parser.add_argument("question")
    search_parser.add_argument("--index-dir", default=DEFAULT_INDEX_DIR)
    search_parser.add_argument("--top-k", type=int, default=5)
    search_parser.set_defaults(handler=_handle_search)

    ask_parser = subparsers.add_parser("ask")
    ask_parser.add_argument("question")
    ask_parser.add_argument("--index-dir", default=DEFAULT_INDEX_DIR)
    ask_parser.add_argument("--top-k", type=int, default=5)
    ask_parser.add_argument(
        "--no-llm",
        action="store_true",
        help="Required explicit marker; this stdlib tool never calls an LLM.",
    )
    ask_parser.set_defaults(handler=_handle_ask)
    return parser


def _handle_index(args: argparse.Namespace) -> int:
    result = build_docs_index(root=args.root, index_dir=args.index_dir)
    print(
        "Indexed "
        f"{result.chunk_count} chunks from {result.file_count} files into "
        f"{_display_path(result.index_dir)}"
    )
    return 0


def _handle_search(args: argparse.Namespace) -> int:
    try:
        results = search_docs(args.question, index_dir=args.index_dir, top_k=args.top_k)
    except FileNotFoundError as exc:
        print(f"Docs index not found: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"Docs search failed: {exc}", file=sys.stderr)
        return 1

    if not results:
        print("No relevant documentation chunks found.")
        return 0

    _warn_if_index_stale(args.index_dir)
    for index, result in enumerate(results, start=1):
        chunk = result.chunk
        heading = f" | {chunk.heading}" if chunk.heading else ""
        print(
            f"{index}. score={result.score:.3f} "
            f"lexical={result.lexical_score:.3f} "
            f"source={chunk.source_type} {chunk.citation}{heading}"
        )
        print(f"   {snippet(chunk.text)}")
    return 0


def _handle_ask(args: argparse.Namespace) -> int:
    try:
        answer = ask_docs(
            args.question,
            index_dir=args.index_dir,
            top_k=args.top_k,
            no_llm=args.no_llm,
        )
    except FileNotFoundError as exc:
        print(f"Docs index not found: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"Docs ask failed: {exc}", file=sys.stderr)
        return 1

    _warn_if_index_stale(args.index_dir)
    print(answer.answer)
    if answer.citations:
        print("\nSources:")
        for citation in answer.citations:
            print(f"- {citation}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
