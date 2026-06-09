from __future__ import annotations

import math
from collections import Counter
from pathlib import Path

from .indexing import (
    DEFAULT_COLLECTION_NAME,
    DEFAULT_INDEX_DIR,
    format_chunk_for_embedding,
    load_chunks,
    load_manifest,
)
from .models import DocChunk, SearchResult
from .text import normalize_identifier, tokenize


def search_docs(
    question: str,
    index_dir: str | Path = DEFAULT_INDEX_DIR,
    *,
    top_k: int = 5,
) -> list[SearchResult]:
    """Search the local docs index with hybrid lexical and dense retrieval."""

    if top_k <= 0:
        raise ValueError("top_k must be positive")
    chunks = load_chunks(index_dir)
    if not chunks:
        return []

    lexical_scores = _lexical_scores(question, chunks)
    dense_scores = _dense_scores(question, chunks, index_dir)
    combined: list[SearchResult] = []
    for chunk in chunks:
        lexical_score = lexical_scores.get(chunk.id, 0.0)
        dense_score = dense_scores.get(chunk.id, 0.0)
        boost = _metadata_boost(question, chunk)
        score = (0.60 * lexical_score) + (0.40 * dense_score) + boost
        if score > 0:
            combined.append(
                SearchResult(
                    chunk=chunk,
                    score=score,
                    lexical_score=lexical_score,
                    dense_score=dense_score,
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


def _lexical_scores(question: str, chunks: list[DocChunk]) -> dict[str, float]:
    query_tokens = tokenize(question)
    if not query_tokens:
        return {}
    corpus_tokens = [tokenize(format_chunk_for_embedding(chunk)) for chunk in chunks]
    rank_bm25_scores = _rank_bm25_scores(query_tokens, corpus_tokens)
    if rank_bm25_scores is None:
        raw_scores = _fallback_bm25_scores(query_tokens, corpus_tokens)
    else:
        raw_scores = rank_bm25_scores
    normalized = _normalize(raw_scores)
    return {chunk.id: score for chunk, score in zip(chunks, normalized)}


def _rank_bm25_scores(
    query_tokens: list[str],
    corpus_tokens: list[list[str]],
) -> list[float] | None:
    try:
        from rank_bm25 import BM25Okapi
    except ImportError:
        return None
    if not corpus_tokens:
        return []
    return [float(score) for score in BM25Okapi(corpus_tokens).get_scores(query_tokens)]


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


def _dense_scores(
    question: str,
    chunks: list[DocChunk],
    index_dir: str | Path,
) -> dict[str, float]:
    manifest = load_manifest(index_dir)
    if not manifest.get("dense_enabled"):
        return {}
    try:
        import chromadb
        from sentence_transformers import SentenceTransformer
    except ImportError:
        return {}

    embedding_model = str(manifest.get("embedding_model") or "")
    if not embedding_model:
        return {}
    collection_name = str(manifest.get("collection_name") or DEFAULT_COLLECTION_NAME)
    try:
        model = SentenceTransformer(embedding_model)
        query_embedding = model.encode(
            [question],
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        if hasattr(query_embedding, "tolist"):
            query_embedding = query_embedding.tolist()
        client = chromadb.PersistentClient(path=str(Path(index_dir) / "chroma"))
        collection = client.get_collection(collection_name)
        result = collection.query(
            query_embeddings=query_embedding,
            n_results=min(len(chunks), max(20, len(chunks))),
            include=["distances"],
        )
    except Exception:
        return {}

    ids = result.get("ids", [[]])
    distances = result.get("distances", [[]])
    if not ids or not distances:
        return {}
    raw_scores: dict[str, float] = {}
    for chunk_id, distance in zip(ids[0], distances[0]):
        raw_scores[str(chunk_id)] = max(0.0, 1.0 - float(distance))
    max_score = max(raw_scores.values(), default=0.0)
    if max_score <= 0:
        return {}
    return {chunk_id: score / max_score for chunk_id, score in raw_scores.items()}


def _metadata_boost(question: str, chunk: DocChunk) -> float:
    query_normalized = normalize_identifier(question)
    query_tokens = set(tokenize(question))
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
    if chunk.is_function_doc and any(token in {"signature", "input", "inputs", "function"} for token in query_tokens):
        boost += 0.10
    if _is_sql_domain_query(query_tokens):
        if chunk.module == "sql" or chunk.path.startswith("docs/AIRFLOW_SQL_MANUAL.md"):
            boost += 0.18
        if (
            chunk.path == "docs/modules/sql/configuration.md"
            and query_tokens & {"configure", "configuration", "connection", "connections", "alias", "aliases"}
        ):
            boost += 0.35
        if _is_docs_assistant_chunk(chunk) and not _is_rag_docs_query(query_tokens):
            boost -= 0.35
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


def _is_docs_assistant_chunk(chunk: DocChunk) -> bool:
    return chunk.path == "docs/RAG_DOCS_ASSISTANT.md" or (
        chunk.path == "README.md" and "local docs assistant" in chunk.heading.lower()
    )


def _is_rag_docs_query(query_tokens: set[str]) -> bool:
    return bool(query_tokens & {"assistant", "index", "rag", "retrieval", "search"})


def _normalize(values: list[float]) -> list[float]:
    max_value = max(values, default=0.0)
    if max_value <= 0:
        return [0.0 for _ in values]
    return [value / max_value for value in values]
