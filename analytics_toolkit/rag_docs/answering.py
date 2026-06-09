from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .indexing import DEFAULT_INDEX_DIR
from .models import SearchResult
from .providers import (
    DEFAULT_OLLAMA_CHAT_MODEL,
    build_generation_provider,
)
from .retrieval import search_docs
from .text import snippet


DEFAULT_OLLAMA_MODEL = DEFAULT_OLLAMA_CHAT_MODEL


@dataclass(frozen=True)
class AnswerResult:
    answer: str
    results: tuple[SearchResult, ...]
    used_llm: bool
    llm_message: str | None = None

    @property
    def citations(self) -> list[str]:
        return [result.chunk.citation for result in self.results]


def ask_docs(
    question: str,
    index_dir: str | Path = DEFAULT_INDEX_DIR,
    *,
    top_k: int = 5,
    llm_provider: str = "ollama",
    model: str | None = None,
    api_key_env: str | None = None,
    base_url: str | None = None,
    timeout: float | None = None,
    embedding_provider: str | None = None,
    embedding_model: str | None = None,
    embedding_api_key_env: str | None = None,
    embedding_base_url: str | None = None,
    embedding_timeout: float | None = None,
    use_llm: bool = True,
) -> AnswerResult:
    """Answer a docs question from retrieved local context."""

    results = tuple(
        search_docs(
            question,
            index_dir,
            top_k=top_k,
            embedding_provider=embedding_provider,
            embedding_model=embedding_model,
            api_key_env=embedding_api_key_env,
            base_url=embedding_base_url,
            timeout=embedding_timeout,
        )
    )
    if not results:
        return AnswerResult(
            answer=(
                "I could not find enough relevant documentation context in the local "
                "RAG index to answer that question."
            ),
            results=(),
            used_llm=False,
            llm_message="No relevant chunks retrieved.",
        )

    context = _format_context(results)
    if use_llm:
        try:
            return AnswerResult(
                answer=_generate_with_provider(
                    question,
                    context,
                    llm_provider=llm_provider,
                    model=model,
                    api_key_env=api_key_env,
                    base_url=base_url,
                    timeout=timeout,
                ),
                results=results,
                used_llm=True,
            )
        except Exception as exc:
            return AnswerResult(
                answer=_fallback_answer(results),
                results=results,
                used_llm=False,
                llm_message=f"{llm_provider} generation unavailable: {exc}",
            )

    return AnswerResult(
        answer=_fallback_answer(results),
        results=results,
        used_llm=False,
        llm_message="LLM generation disabled.",
    )


def _generate_with_provider(
    question: str,
    context: str,
    *,
    llm_provider: str,
    model: str | None,
    api_key_env: str | None,
    base_url: str | None,
    timeout: float | None,
) -> str:
    provider = build_generation_provider(
        llm_provider,
        model=model,
        api_key_env=api_key_env,
        base_url=base_url,
        timeout=timeout,
    )
    return provider.answer(question, context)


def _format_context(results: tuple[SearchResult, ...]) -> str:
    parts: list[str] = []
    for index, result in enumerate(results, start=1):
        chunk = result.chunk
        heading = f" ({chunk.heading})" if chunk.heading else ""
        parts.append(
            f"[{index}] {chunk.citation}{heading}\n{chunk.text.strip()}"
        )
    return "\n\n".join(parts)


def _fallback_answer(results: tuple[SearchResult, ...]) -> str:
    lines = [
        "I found relevant local documentation, but local LLM generation is not being used.",
        "Most relevant passages:",
    ]
    for index, result in enumerate(results, start=1):
        chunk = result.chunk
        heading = f" - {chunk.heading}" if chunk.heading else ""
        lines.append(
            f"[{index}] {chunk.citation}{heading}: {snippet(chunk.text, 360)}"
        )
    return "\n".join(lines)
