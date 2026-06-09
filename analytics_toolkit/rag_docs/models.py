from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DocChunk:
    """A retrievable documentation section."""

    id: str
    path: str
    heading_path: tuple[str, ...]
    line_start: int
    line_end: int
    text: str
    module: str | None = None
    function_name: str | None = None
    is_function_doc: bool = False

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
        )


@dataclass(frozen=True)
class SearchResult:
    """A ranked retrieval result."""

    chunk: DocChunk
    score: float
    lexical_score: float = 0.0
    dense_score: float = 0.0


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None
