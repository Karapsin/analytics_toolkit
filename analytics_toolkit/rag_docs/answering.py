from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .indexing import DEFAULT_INDEX_DIR
from .models import SearchResult
from .retrieval import search_docs
from .text import snippet


DEFAULT_OLLAMA_MODEL = "llama3.1"


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
    model: str = DEFAULT_OLLAMA_MODEL,
    use_llm: bool = True,
) -> AnswerResult:
    """Answer a docs question from retrieved local context."""

    results = tuple(search_docs(question, index_dir, top_k=top_k))
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
                answer=_generate_with_ollama(question, context, model),
                results=results,
                used_llm=True,
            )
        except Exception as exc:
            return AnswerResult(
                answer=_fallback_answer(results),
                results=results,
                used_llm=False,
                llm_message=f"Ollama generation unavailable: {exc}",
            )

    return AnswerResult(
        answer=_fallback_answer(results),
        results=results,
        used_llm=False,
        llm_message="LLM generation disabled.",
    )


def _generate_with_ollama(question: str, context: str, model: str) -> str:
    try:
        import ollama
    except ImportError as exc:
        raise RuntimeError("install analytics-toolkit[rag] to enable Ollama generation") from exc

    response = ollama.chat(
        model=model,
        messages=[
            {
                "role": "system",
                "content": (
                    "Answer only from the provided analytics_toolkit documentation "
                    "context. If the context is insufficient, say that the docs do "
                    "not contain enough information. Cite sources with bracketed "
                    "numbers like [1]."
                ),
            },
            {
                "role": "user",
                "content": f"Question:\n{question}\n\nDocumentation context:\n{context}",
            },
        ],
    )
    message = response.get("message", {})
    content = message.get("content", "")
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("Ollama returned an empty response")
    return content.strip()


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
