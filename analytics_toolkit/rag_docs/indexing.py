from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .chunking import DEFAULT_MAX_CHARS, DEFAULT_OVERLAP_CHARS, chunk_markdown_file
from .discovery import discover_markdown_files
from .models import DocChunk


INDEX_VERSION = 1
DEFAULT_INDEX_DIR = ".rag_index"
DEFAULT_COLLECTION_NAME = "analytics_toolkit_docs"
DEFAULT_EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"


@dataclass(frozen=True)
class IndexBuildResult:
    index_dir: Path
    file_count: int
    chunk_count: int
    dense_enabled: bool
    dense_message: str | None = None


def build_docs_index(
    root: str | Path = ".",
    index_dir: str | Path = DEFAULT_INDEX_DIR,
    *,
    dense: bool = True,
    embedding_model: str = DEFAULT_EMBEDDING_MODEL,
    max_chars: int = DEFAULT_MAX_CHARS,
    overlap_chars: int = DEFAULT_OVERLAP_CHARS,
) -> IndexBuildResult:
    """Build a local docs index with lexical data and optional dense vectors."""

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
        {
            "version": INDEX_VERSION,
            "chunks": [chunk.to_dict() for chunk in chunks],
        },
    )

    dense_enabled = False
    dense_message: str | None = None
    if dense:
        dense_enabled, dense_message = _build_dense_index(
            index_path,
            chunks,
            embedding_model=embedding_model,
        )
    else:
        dense_message = "Dense retrieval disabled by --no-dense."

    _write_json(
        index_path / "manifest.json",
        {
            "version": INDEX_VERSION,
            "root": str(root_path),
            "file_count": len(files),
            "chunk_count": len(chunks),
            "dense_enabled": dense_enabled,
            "dense_message": dense_message,
            "embedding_model": embedding_model if dense_enabled else None,
            "collection_name": DEFAULT_COLLECTION_NAME if dense_enabled else None,
        },
    )
    return IndexBuildResult(
        index_dir=index_path,
        file_count=len(files),
        chunk_count=len(chunks),
        dense_enabled=dense_enabled,
        dense_message=dense_message,
    )


def load_chunks(index_dir: str | Path = DEFAULT_INDEX_DIR) -> list[DocChunk]:
    data = _read_json(Path(index_dir) / "chunks.json")
    chunks = data.get("chunks", [])
    if not isinstance(chunks, list):
        raise ValueError("Invalid docs index: chunks must be a list")
    return [DocChunk.from_dict(chunk) for chunk in chunks if isinstance(chunk, dict)]


def load_manifest(index_dir: str | Path = DEFAULT_INDEX_DIR) -> dict[str, Any]:
    manifest_path = Path(index_dir) / "manifest.json"
    if not manifest_path.is_file():
        return {}
    manifest = _read_json(manifest_path)
    return manifest if isinstance(manifest, dict) else {}


def format_chunk_for_embedding(chunk: DocChunk) -> str:
    metadata = [
        f"Path: {chunk.path}",
        f"Heading: {chunk.heading}" if chunk.heading else "",
        f"Module: {chunk.module}" if chunk.module else "",
        f"Function: {chunk.function_name}" if chunk.function_name else "",
    ]
    metadata_text = "\n".join(part for part in metadata if part)
    return f"{metadata_text}\n\n{chunk.text}".strip()


def _build_dense_index(
    index_path: Path,
    chunks: list[DocChunk],
    *,
    embedding_model: str,
) -> tuple[bool, str | None]:
    if not chunks:
        return False, "No documentation chunks to embed."
    try:
        import chromadb
        from sentence_transformers import SentenceTransformer
    except ImportError as exc:
        return (
            False,
            "Dense retrieval skipped because optional RAG dependencies are missing: "
            f"{exc.name}. Install analytics-toolkit[rag].",
        )

    try:
        model = SentenceTransformer(embedding_model)
        texts = [format_chunk_for_embedding(chunk) for chunk in chunks]
        embeddings = model.encode(
            texts,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        client = chromadb.PersistentClient(path=str(index_path / "chroma"))
        try:
            client.delete_collection(DEFAULT_COLLECTION_NAME)
        except Exception:
            pass
        collection = client.create_collection(
            DEFAULT_COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"},
        )
        collection.add(
            ids=[chunk.id for chunk in chunks],
            documents=texts,
            embeddings=_to_embedding_list(embeddings),
            metadatas=[_chroma_metadata(chunk) for chunk in chunks],
        )
    except Exception as exc:
        return False, f"Dense retrieval skipped after indexing error: {exc}"
    return True, f"Dense retrieval enabled with embedding model {embedding_model}."


def _chroma_metadata(chunk: DocChunk) -> dict[str, str | int | bool]:
    return {
        "path": chunk.path,
        "heading": chunk.heading,
        "line_start": chunk.line_start,
        "line_end": chunk.line_end,
        "module": chunk.module or "",
        "function_name": chunk.function_name or "",
        "is_function_doc": chunk.is_function_doc,
    }


def _to_embedding_list(value: Any) -> list[list[float]]:
    if hasattr(value, "tolist"):
        value = value.tolist()
    return [[float(item) for item in row] for row in value]


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
