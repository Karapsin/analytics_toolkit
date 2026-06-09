"""Local documentation retrieval helpers for the analytics-toolkit CLI."""

from .answering import AnswerResult, ask_docs
from .chunking import chunk_markdown_file
from .discovery import discover_markdown_files
from .indexing import IndexBuildResult, build_docs_index
from .models import DocChunk, SearchResult
from .retrieval import search_docs

__all__ = [
    "AnswerResult",
    "DocChunk",
    "IndexBuildResult",
    "SearchResult",
    "ask_docs",
    "build_docs_index",
    "chunk_markdown_file",
    "discover_markdown_files",
    "search_docs",
]
