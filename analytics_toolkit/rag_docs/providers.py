from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any


DEFAULT_SENTENCE_TRANSFORMERS_MODEL = "BAAI/bge-small-en-v1.5"
DEFAULT_OLLAMA_CHAT_MODEL = "llama3.1"
DEFAULT_OLLAMA_EMBEDDING_MODEL = "nomic-embed-text"
DEFAULT_OPENAI_EMBEDDING_MODEL = "text-embedding-3-small"
DEFAULT_GEMINI_EMBEDDING_MODEL = "gemini-embedding-001"

EMBEDDING_PROVIDERS = {
    "sentence-transformers",
    "ollama",
    "openai",
    "gemini",
    "openai-compatible",
}
GENERATION_PROVIDERS = {
    "ollama",
    "openai",
    "anthropic",
    "gemini",
    "openai-compatible",
}


class RagProviderError(RuntimeError):
    """Raised when a RAG provider cannot be configured or called."""


@dataclass(frozen=True)
class EmbeddingProviderConfig:
    provider: str
    model: str
    api_key_env: str | None = None
    base_url: str | None = None
    timeout: float | None = None


@dataclass(frozen=True)
class GenerationProviderConfig:
    provider: str
    model: str
    api_key_env: str | None = None
    base_url: str | None = None
    timeout: float | None = None


class EmbeddingProvider:
    config: EmbeddingProviderConfig

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]


class GenerationProvider:
    config: GenerationProviderConfig

    def answer(self, question: str, context: str) -> str:
        raise NotImplementedError


def normalize_embedding_provider(provider: str | None) -> str:
    normalized = _normalize_provider(provider or "sentence-transformers")
    aliases = {
        "local": "sentence-transformers",
        "sentence_transformers": "sentence-transformers",
        "sentence-transformer": "sentence-transformers",
        "st": "sentence-transformers",
        "openai_compatible": "openai-compatible",
        "openai-compatible": "openai-compatible",
        "google": "gemini",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in EMBEDDING_PROVIDERS:
        raise RagProviderError(
            f"Unsupported embedding provider {provider!r}. "
            f"Use one of: {', '.join(sorted(EMBEDDING_PROVIDERS))}."
        )
    return normalized


def normalize_generation_provider(provider: str | None) -> str:
    normalized = _normalize_provider(provider or "ollama")
    aliases = {
        "local": "ollama",
        "openai_compatible": "openai-compatible",
        "openai-compatible": "openai-compatible",
        "google": "gemini",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in GENERATION_PROVIDERS:
        raise RagProviderError(
            f"Unsupported LLM provider {provider!r}. "
            f"Use one of: {', '.join(sorted(GENERATION_PROVIDERS))}."
        )
    return normalized


def default_embedding_model(provider: str) -> str:
    normalized = normalize_embedding_provider(provider)
    defaults = {
        "sentence-transformers": DEFAULT_SENTENCE_TRANSFORMERS_MODEL,
        "ollama": DEFAULT_OLLAMA_EMBEDDING_MODEL,
        "openai": DEFAULT_OPENAI_EMBEDDING_MODEL,
        "openai-compatible": DEFAULT_OPENAI_EMBEDDING_MODEL,
        "gemini": DEFAULT_GEMINI_EMBEDDING_MODEL,
    }
    return defaults[normalized]


def default_generation_model(provider: str) -> str | None:
    normalized = normalize_generation_provider(provider)
    if normalized == "ollama":
        return DEFAULT_OLLAMA_CHAT_MODEL
    return None


def build_embedding_provider(
    provider: str | None = None,
    *,
    model: str | None = None,
    api_key_env: str | None = None,
    base_url: str | None = None,
    timeout: float | None = None,
) -> EmbeddingProvider:
    normalized = normalize_embedding_provider(provider)
    config = EmbeddingProviderConfig(
        provider=normalized,
        model=model or default_embedding_model(normalized),
        api_key_env=api_key_env,
        base_url=_clean_optional(base_url),
        timeout=timeout,
    )
    if normalized == "sentence-transformers":
        return SentenceTransformersEmbeddingProvider(config)
    if normalized == "ollama":
        return OllamaEmbeddingProvider(config)
    if normalized in {"openai", "openai-compatible"}:
        return OpenAIEmbeddingProvider(config)
    if normalized == "gemini":
        return GeminiEmbeddingProvider(config)
    raise AssertionError(f"Unhandled embedding provider {normalized}")


def build_generation_provider(
    provider: str | None = None,
    *,
    model: str | None = None,
    api_key_env: str | None = None,
    base_url: str | None = None,
    timeout: float | None = None,
) -> GenerationProvider:
    normalized = normalize_generation_provider(provider)
    resolved_model = model or default_generation_model(normalized)
    if not resolved_model:
        raise RagProviderError(
            f"--model is required when --llm-provider is {normalized!r}."
        )
    config = GenerationProviderConfig(
        provider=normalized,
        model=resolved_model,
        api_key_env=api_key_env,
        base_url=_clean_optional(base_url),
        timeout=timeout,
    )
    if normalized == "ollama":
        return OllamaGenerationProvider(config)
    if normalized in {"openai", "openai-compatible"}:
        return OpenAIGenerationProvider(config)
    if normalized == "anthropic":
        return AnthropicGenerationProvider(config)
    if normalized == "gemini":
        return GeminiGenerationProvider(config)
    raise AssertionError(f"Unhandled LLM provider {normalized}")


class SentenceTransformersEmbeddingProvider(EmbeddingProvider):
    def __init__(self, config: EmbeddingProviderConfig) -> None:
        self.config = config

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise RagProviderError(
                "Install analytics-toolkit[rag] to use sentence-transformers embeddings."
            ) from exc
        model = SentenceTransformer(self.config.model)
        embeddings = model.encode(
            texts,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        return _to_embedding_list(embeddings)


class OllamaEmbeddingProvider(EmbeddingProvider):
    def __init__(self, config: EmbeddingProviderConfig) -> None:
        self.config = config

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        try:
            import ollama
        except ImportError as exc:
            raise RagProviderError(
                "Install analytics-toolkit[rag] to use Ollama embeddings."
            ) from exc

        client = _ollama_client(ollama, self.config.base_url)
        response = _call_ollama_embed(client, ollama, self.config.model, texts)
        embeddings = _extract_key(response, "embeddings")
        return _to_embedding_list(embeddings)


class OpenAIEmbeddingProvider(EmbeddingProvider):
    def __init__(self, config: EmbeddingProviderConfig) -> None:
        self.config = config

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise RagProviderError(
                "Install analytics-toolkit[rag-openai] to use OpenAI embeddings."
            ) from exc

        client = OpenAI(**_openai_client_kwargs(self.config, default_env="OPENAI_API_KEY"))
        response = client.embeddings.create(model=self.config.model, input=texts)
        data = _extract_key(response, "data")
        return [_extract_embedding(item) for item in data]


class GeminiEmbeddingProvider(EmbeddingProvider):
    def __init__(self, config: EmbeddingProviderConfig) -> None:
        self.config = config

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        try:
            from google import genai
        except ImportError as exc:
            raise RagProviderError(
                "Install analytics-toolkit[rag-gemini] to use Gemini embeddings."
            ) from exc

        client = genai.Client(
            api_key=_resolve_api_key(self.config.api_key_env, "GEMINI_API_KEY")
        )
        response = client.models.embed_content(model=self.config.model, contents=texts)
        embeddings = _extract_key(response, "embeddings")
        return [_extract_values(item) for item in embeddings]


class OllamaGenerationProvider(GenerationProvider):
    def __init__(self, config: GenerationProviderConfig) -> None:
        self.config = config

    def answer(self, question: str, context: str) -> str:
        try:
            import ollama
        except ImportError as exc:
            raise RagProviderError(
                "Install analytics-toolkit[rag] to use Ollama generation."
            ) from exc

        client = _ollama_client(ollama, self.config.base_url)
        response = client.chat(
            model=self.config.model,
            messages=_rag_messages(question, context),
        )
        message = _extract_key(response, "message")
        return _extract_text(_extract_key(message, "content"))


class OpenAIGenerationProvider(GenerationProvider):
    def __init__(self, config: GenerationProviderConfig) -> None:
        self.config = config

    def answer(self, question: str, context: str) -> str:
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise RagProviderError(
                "Install analytics-toolkit[rag-openai] to use OpenAI generation."
            ) from exc

        client = OpenAI(**_openai_client_kwargs(self.config, default_env="OPENAI_API_KEY"))
        if self.config.provider == "openai" and hasattr(client, "responses"):
            response = client.responses.create(
                model=self.config.model,
                input=_rag_messages(question, context),
            )
            return _extract_openai_response_text(response)

        response = client.chat.completions.create(
            model=self.config.model,
            messages=_rag_messages(question, context),
        )
        return _extract_text(response.choices[0].message.content)


class AnthropicGenerationProvider(GenerationProvider):
    def __init__(self, config: GenerationProviderConfig) -> None:
        self.config = config

    def answer(self, question: str, context: str) -> str:
        try:
            from anthropic import Anthropic
        except ImportError as exc:
            raise RagProviderError(
                "Install analytics-toolkit[rag-anthropic] to use Anthropic generation."
            ) from exc

        client = Anthropic(
            api_key=_resolve_api_key(self.config.api_key_env, "ANTHROPIC_API_KEY")
        )
        response = client.messages.create(
            model=self.config.model,
            max_tokens=1200,
            system=_rag_system_prompt(),
            messages=[{"role": "user", "content": _rag_user_prompt(question, context)}],
        )
        content = _extract_key(response, "content")
        return _extract_text(_join_anthropic_content(content))


class GeminiGenerationProvider(GenerationProvider):
    def __init__(self, config: GenerationProviderConfig) -> None:
        self.config = config

    def answer(self, question: str, context: str) -> str:
        try:
            from google import genai
        except ImportError as exc:
            raise RagProviderError(
                "Install analytics-toolkit[rag-gemini] to use Gemini generation."
            ) from exc

        client = genai.Client(
            api_key=_resolve_api_key(self.config.api_key_env, "GEMINI_API_KEY")
        )
        response = client.models.generate_content(
            model=self.config.model,
            contents=f"{_rag_system_prompt()}\n\n{_rag_user_prompt(question, context)}",
        )
        return _extract_text(_extract_key(response, "text"))


def _rag_messages(question: str, context: str) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": _rag_system_prompt()},
        {"role": "user", "content": _rag_user_prompt(question, context)},
    ]


def _rag_system_prompt() -> str:
    return (
        "Answer only from the provided analytics_toolkit documentation context. "
        "If the context is insufficient, say that the docs do not contain enough "
        "information. Cite sources with bracketed numbers like [1]."
    )


def _rag_user_prompt(question: str, context: str) -> str:
    return f"Question:\n{question}\n\nDocumentation context:\n{context}"


def _openai_client_kwargs(
    config: EmbeddingProviderConfig | GenerationProviderConfig,
    *,
    default_env: str,
) -> dict[str, object]:
    kwargs: dict[str, object] = {
        "api_key": _resolve_api_key(config.api_key_env, default_env),
    }
    if config.base_url:
        kwargs["base_url"] = config.base_url
    if config.timeout is not None:
        kwargs["timeout"] = config.timeout
    if config.provider == "openai-compatible" and not config.base_url:
        raise RagProviderError("--base-url is required for openai-compatible providers.")
    return kwargs


def _resolve_api_key(api_key_env: str | None, default_env: str) -> str:
    env_name = api_key_env or default_env
    value = os.environ.get(env_name)
    if not value:
        raise RagProviderError(f"Environment variable {env_name} is required.")
    return value


def _ollama_client(ollama: Any, base_url: str | None) -> Any:
    if base_url and hasattr(ollama, "Client"):
        return ollama.Client(host=base_url)
    if hasattr(ollama, "Client"):
        return ollama.Client()
    return ollama


def _call_ollama_embed(client: Any, ollama: Any, model: str, texts: list[str]) -> Any:
    if hasattr(client, "embed"):
        return client.embed(model=model, input=texts)
    if hasattr(ollama, "embed"):
        return ollama.embed(model=model, input=texts)
    embeddings = []
    for text in texts:
        if hasattr(client, "embeddings"):
            embeddings.append(_extract_key(client.embeddings(model=model, prompt=text), "embedding"))
        else:
            embeddings.append(_extract_key(ollama.embeddings(model=model, prompt=text), "embedding"))
    return {"embeddings": embeddings}


def _extract_openai_response_text(response: Any) -> str:
    output_text = _maybe_extract_key(response, "output_text")
    if output_text:
        return _extract_text(output_text)
    output = _maybe_extract_key(response, "output") or []
    parts: list[str] = []
    for item in output:
        for content in _maybe_extract_key(item, "content") or []:
            text = _maybe_extract_key(content, "text")
            if text:
                parts.append(str(text))
    return _extract_text("\n".join(parts))


def _join_anthropic_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for item in content or []:
        text = _maybe_extract_key(item, "text")
        if text:
            parts.append(str(text))
    return "\n".join(parts)


def _extract_embedding(item: Any) -> list[float]:
    return _to_float_list(_extract_key(item, "embedding"))


def _extract_values(item: Any) -> list[float]:
    return _to_float_list(_extract_key(item, "values"))


def _extract_text(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RagProviderError("Provider returned an empty text response.")
    return value.strip()


def _extract_key(value: Any, key: str) -> Any:
    result = _maybe_extract_key(value, key)
    if result is None:
        raise RagProviderError(f"Provider response is missing {key!r}.")
    return result


def _maybe_extract_key(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        return value.get(key)
    return getattr(value, key, None)


def _to_embedding_list(value: Any) -> list[list[float]]:
    if hasattr(value, "tolist"):
        value = value.tolist()
    return [_to_float_list(row) for row in value]


def _to_float_list(value: Any) -> list[float]:
    if hasattr(value, "tolist"):
        value = value.tolist()
    return [float(item) for item in value]


def _normalize_provider(provider: str) -> str:
    return provider.strip().lower().replace("_", "-")


def _clean_optional(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None
