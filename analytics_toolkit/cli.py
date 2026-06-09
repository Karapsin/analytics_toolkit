from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path


def _prefer_local_package_path() -> None:
    package = sys.modules.get(__package__)
    package_path = getattr(package, "__path__", None)
    if package_path is None:
        return

    local_path = str(Path(__file__).resolve().parent)
    existing_paths = [str(path) for path in package_path]
    package.__path__ = [
        local_path,
        *[path for path in existing_paths if not _same_path(path, local_path)],
    ]


def _same_path(left: str, right: str) -> bool:
    try:
        return Path(left).resolve() == Path(right).resolve()
    except OSError:
        return left == right


_prefer_local_package_path()

from .sql import format_support_matrix
from .sql.connection import validate_connections


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "handler"):
        parser.print_help()
        return 2
    return int(args.handler(args))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="analytics-toolkit")
    subparsers = parser.add_subparsers(dest="command")

    sql_parser = subparsers.add_parser("sql")
    sql_subparsers = sql_parser.add_subparsers(dest="sql_command")

    validate_parser = sql_subparsers.add_parser("validate")
    validate_parser.add_argument("keys", nargs="*")
    validate_parser.add_argument(
        "--connect",
        action="store_true",
        help="Open and close each configured connection after validating config.",
    )
    validate_parser.set_defaults(handler=_handle_sql_validate)

    support_parser = sql_subparsers.add_parser("support-matrix")
    support_parser.set_defaults(handler=_handle_sql_support_matrix)

    docs_parser = subparsers.add_parser("docs")
    docs_subparsers = docs_parser.add_subparsers(dest="docs_command")

    docs_index_parser = docs_subparsers.add_parser("index")
    docs_index_parser.add_argument("--root", default=".")
    docs_index_parser.add_argument("--index-dir", default=".rag_index")
    docs_index_parser.add_argument(
        "--embedding-provider",
        default="sentence-transformers",
        help=(
            "Embedding provider: sentence-transformers, ollama, openai, "
            "gemini, or openai-compatible."
        ),
    )
    docs_index_parser.add_argument(
        "--embedding-model",
        default=None,
        help="Embedding model. Defaults depend on --embedding-provider.",
    )
    docs_index_parser.add_argument(
        "--api-key-env",
        default=None,
        help="Environment variable holding the embedding provider API key.",
    )
    docs_index_parser.add_argument(
        "--base-url",
        default=None,
        help="Embedding provider base URL for compatible/local endpoints.",
    )
    docs_index_parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="Embedding provider request timeout in seconds.",
    )
    docs_index_parser.add_argument(
        "--no-dense",
        action="store_true",
        help="Build only the lexical retrieval index.",
    )
    docs_index_parser.set_defaults(handler=_handle_docs_index)

    docs_search_parser = docs_subparsers.add_parser("search")
    docs_search_parser.add_argument("question")
    docs_search_parser.add_argument("--index-dir", default=".rag_index")
    docs_search_parser.add_argument("--top-k", type=int, default=5)
    docs_search_parser.add_argument("--embedding-provider", default=None)
    docs_search_parser.add_argument("--embedding-model", default=None)
    docs_search_parser.add_argument("--api-key-env", default=None)
    docs_search_parser.add_argument("--base-url", default=None)
    docs_search_parser.add_argument("--timeout", type=float, default=None)
    docs_search_parser.set_defaults(handler=_handle_docs_search)

    docs_ask_parser = docs_subparsers.add_parser("ask")
    docs_ask_parser.add_argument("question")
    docs_ask_parser.add_argument("--index-dir", default=".rag_index")
    docs_ask_parser.add_argument("--top-k", type=int, default=5)
    docs_ask_parser.add_argument(
        "--llm-provider",
        default="ollama",
        help=(
            "Generation provider: ollama, openai, anthropic, gemini, "
            "or openai-compatible."
        ),
    )
    docs_ask_parser.add_argument(
        "--model",
        default=None,
        help="Generation model. Defaults to llama3.1 for Ollama; required for hosted providers.",
    )
    docs_ask_parser.add_argument(
        "--api-key-env",
        default=None,
        help="Environment variable holding the generation provider API key.",
    )
    docs_ask_parser.add_argument(
        "--base-url",
        default=None,
        help="Generation provider base URL for compatible/local endpoints.",
    )
    docs_ask_parser.add_argument("--timeout", type=float, default=None)
    docs_ask_parser.add_argument("--embedding-provider", default=None)
    docs_ask_parser.add_argument("--embedding-model", default=None)
    docs_ask_parser.add_argument("--embedding-api-key-env", default=None)
    docs_ask_parser.add_argument("--embedding-base-url", default=None)
    docs_ask_parser.add_argument("--embedding-timeout", type=float, default=None)
    docs_ask_parser.add_argument(
        "--no-llm",
        action="store_true",
        help="Return retrieved documentation snippets without calling Ollama.",
    )
    docs_ask_parser.set_defaults(handler=_handle_docs_ask)
    return parser


def _handle_sql_validate(args: argparse.Namespace) -> int:
    results = validate_connections(args.keys or None, connect=args.connect)
    for result in results:
        status = "OK" if result.valid else "ERROR"
        backend = result.backend or "-"
        connected = ""
        if args.connect:
            connected = " connected=yes" if result.connected else " connected=no"
        line = f"{status} {result.connection_key} backend={backend}{connected}"
        if result.error:
            line += f" error={result.error}"
        print(line)
    return 0 if all(result.valid for result in results) else 1


def _handle_sql_support_matrix(args: argparse.Namespace) -> int:
    del args
    print(format_support_matrix())
    return 0


def _handle_docs_index(args: argparse.Namespace) -> int:
    from .rag_docs import build_docs_index
    from .rag_docs.providers import RagProviderError

    try:
        result = build_docs_index(
            root=args.root,
            index_dir=args.index_dir,
            dense=not args.no_dense,
            embedding_provider=args.embedding_provider,
            embedding_model=args.embedding_model,
            api_key_env=args.api_key_env,
            base_url=args.base_url,
            timeout=args.timeout,
        )
    except RagProviderError as exc:
        print(f"Docs index failed: {exc}", file=sys.stderr)
        return 1
    print(
        "Indexed "
        f"{result.chunk_count} chunks from {result.file_count} files into "
        f"{_display_path(result.index_dir)}"
    )
    if result.dense_message:
        print(result.dense_message)
    return 0


def _handle_docs_search(args: argparse.Namespace) -> int:
    from .rag_docs import search_docs
    from .rag_docs.providers import RagProviderError
    from .rag_docs.text import snippet

    try:
        results = search_docs(
            args.question,
            index_dir=args.index_dir,
            top_k=args.top_k,
            embedding_provider=args.embedding_provider,
            embedding_model=args.embedding_model,
            api_key_env=args.api_key_env,
            base_url=args.base_url,
            timeout=args.timeout,
        )
    except FileNotFoundError as exc:
        print(f"Docs index not found: {exc}", file=sys.stderr)
        return 1
    except (RagProviderError, ValueError) as exc:
        print(f"Docs search failed: {exc}", file=sys.stderr)
        return 1

    if not results:
        print("No relevant documentation chunks found.")
        return 0

    for index, result in enumerate(results, start=1):
        chunk = result.chunk
        heading = f" | {chunk.heading}" if chunk.heading else ""
        print(
            f"{index}. score={result.score:.3f} "
            f"lexical={result.lexical_score:.3f} dense={result.dense_score:.3f} "
            f"{chunk.citation}{heading}"
        )
        print(f"   {snippet(chunk.text)}")
    return 0


def _handle_docs_ask(args: argparse.Namespace) -> int:
    from .rag_docs import ask_docs
    from .rag_docs.providers import RagProviderError

    try:
        answer = ask_docs(
            args.question,
            index_dir=args.index_dir,
            top_k=args.top_k,
            llm_provider=args.llm_provider,
            model=args.model,
            api_key_env=args.api_key_env,
            base_url=args.base_url,
            timeout=args.timeout,
            embedding_provider=args.embedding_provider,
            embedding_model=args.embedding_model,
            embedding_api_key_env=args.embedding_api_key_env,
            embedding_base_url=args.embedding_base_url,
            embedding_timeout=args.embedding_timeout,
            use_llm=not args.no_llm,
        )
    except FileNotFoundError as exc:
        print(f"Docs index not found: {exc}", file=sys.stderr)
        return 1
    except (RagProviderError, ValueError) as exc:
        print(f"Docs ask failed: {exc}", file=sys.stderr)
        return 1

    print(answer.answer)
    if answer.llm_message:
        print(f"\nNote: {answer.llm_message}")
    if answer.citations:
        print("\nSources:")
        for citation in answer.citations:
            print(f"- {citation}")
    return 0


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(Path.cwd()))
    except ValueError:
        return str(path)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
