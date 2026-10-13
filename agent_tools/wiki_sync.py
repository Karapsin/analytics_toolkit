"""Export repository user documentation into a separately cloned GitHub wiki."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import NoReturn
from urllib.parse import quote, unquote, urlsplit

REPOSITORY = "https://github.com/Karapsin/analytics_toolkit"
MANIFEST = ".wiki-sync-manifest.json"
TOP_LEVEL = ("README.md", "QUICK_START.md", "AIRFLOW_SQL_MANUAL.md", "CHANGELOG.md")
# Match Markdown destinations, including angle brackets and balanced parentheses.
DESTINATION = r"(?:<[^>\n]+>|(?:[^\s()]|\([^()]*\))+?)"
LINK = re.compile(r"!?\[[^\]\n]*\]\(\s*(?P<url>" + DESTINATION + r")(?=\s|\))")
REFERENCE = re.compile(r"(?m)^ {0,3}\[[^\]\n]+\]:\s*(?P<url>" + DESTINATION + r")(?=\s|$)")


def fail(message: str) -> NoReturn:
    raise ValueError(message)


def code_spans(text: str) -> list[tuple[int, int]]:
    """Protect fenced, indented and inline examples from URL rewriting."""
    spans = []
    fence = None
    start = 0
    offset = 0
    for line in text.splitlines(keepends=True):
        match = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if fence is None and match:
            fence = match.group(1)
            start = offset
        elif fence is not None and match:
            marker = match.group(1)
            if (
                marker[0] == fence[0]
                and len(marker) >= len(fence)
                and not line[match.end() :].strip()
            ):
                spans.append((start, offset + len(line)))
                fence = None
        elif fence is None and (line.startswith(("    ", "\t"))):
            spans.append((offset, offset + len(line)))
        offset += len(line)
    if fence is not None:
        spans.append((start, len(text)))
    masked = list(text)
    for start, end in spans:
        masked[start:end] = " " * (end - start)
    spans.extend(
        match.span() for match in re.finditer(r"(`+)(?!`)([\s\S]*?)(?<!`)\1(?!`)", "".join(masked))
    )
    return spans


def anchors(text: str) -> set[str]:
    spans = code_spans(text)
    seen: dict[str, int] = {}
    result = set()
    for match in re.finditer(r"(?m)^ {0,3}#{1,6}\s+(.+?)\s*#*\s*$", text):
        if any(start <= match.start() < end for start, end in spans):
            continue
        title = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", match.group(1))
        title = re.sub(r"<[^>]*>", "", title).lower()
        slug = "".join(c for c in title if c.isalnum() or c in "_- ").replace(" ", "-")
        count = seen.get(slug, 0)
        seen[slug] = count + 1
        result.add(slug if count == 0 else f"{slug}-{count}")
    result.update(re.findall(r'<(?:a|\w+)\s+[^>]*(?:id|name)=[\'"]([^\'"]+)', text))
    return result


def page_name(relative: Path) -> str:
    if relative == Path("README.md"):
        return "Home"
    if relative == Path("modules/README.md"):
        return "Modules"
    parts = relative.with_suffix("").parts
    return "-".join(parts[1:] if parts[0] == "modules" else parts)


def read_sources(source: Path) -> tuple[dict[Path, str], dict[Path, str]]:
    """Select public documentation and reject ambiguous page names."""
    source = source.resolve()
    paths = [source / name for name in TOP_LEVEL]
    paths.extend(sorted((source / "modules").rglob("*.md")))
    names: dict[Path, str] = {}
    texts: dict[Path, str] = {}
    used: set[str] = {"_sidebar"}
    for path in paths:
        if path.is_symlink():
            fail(f"Documentation symlink is unsupported: {path}")
        name = page_name(path.relative_to(source))
        if name.casefold() in used or not re.fullmatch(r"[\w-]+", name):
            fail(f"Invalid or colliding wiki page name: {name}")
        used.add(name.casefold())
        names[path] = name
        texts[path] = path.read_text(encoding="utf-8")
    home = source / "README.md"
    texts[home] = texts[home].split("## Agent workflows")[0]
    texts[home] = (
        "\n".join(line for line in texts[home].splitlines() if "INTEGRATION_TESTING.md" not in line)
        + "\n"
    )
    return names, texts


def rewrite_url(
    raw: str, path: Path, root: Path, names: dict[Path, str], heading_ids: dict[Path, set[str]]
) -> str:
    angle = raw.startswith("<")
    url = raw[1:-1] if angle else raw
    parsed = urlsplit(url)
    if parsed.scheme or parsed.netloc:
        return raw
    target = (path.parent / unquote(parsed.path)).resolve() if parsed.path else path
    try:
        relative = target.relative_to(root)
    except ValueError as exc:
        message = f"Link escapes repository in {path}: {url}"
        raise ValueError(message) from exc
    if not target.exists():
        fail(f"Missing link target in {path}: {url}")
    fragment = "#" + parsed.fragment if parsed.fragment else ""
    query = "?" + parsed.query if parsed.query else ""
    if target in names:
        if parsed.fragment and unquote(parsed.fragment) not in heading_ids[target]:
            fail(f"Missing heading in {path}: {url}")
        rewritten = REPOSITORY + "/wiki/" + quote(names[target]) + query + fragment
    else:
        endpoint = (
            "raw"
            if re.search(r"\.(png|jpg|jpeg|gif|svg|webp)$", parsed.path, re.IGNORECASE)
            else "blob"
        )
        rewritten = f"{REPOSITORY}/{endpoint}/main/{quote(relative.as_posix())}{query}{fragment}"
    return "<" + rewritten + ">" if angle else rewritten


def export_pages(source: Path) -> dict[str, str]:
    """Build and validate every page before changing the output checkout."""
    source = source.resolve()
    names, texts = read_sources(source)
    heading_ids = {path: anchors(text) for path, text in texts.items()}
    result: dict[str, str] = {}
    for path, original_text in texts.items():
        text = original_text
        protected = code_spans(text)
        changes = []
        for pattern in (LINK, REFERENCE):
            for match in pattern.finditer(text):
                start, end = match.span("url")
                if any(left <= start < right for left, right in protected):
                    continue
                changes.append(
                    (
                        start,
                        end,
                        rewrite_url(match.group("url"), path, source.parent, names, heading_ids),
                    )
                )
        for start, end, value in sorted(changes, reverse=True):
            text = text[:start] + value + text[end:]
        original = path.relative_to(source.parent).as_posix()
        notice = (
            f"> Generated from [repository documentation]({REPOSITORY}/blob/main/{original}). "
            "Edit that source; wiki edits to this page are replaced during synchronization.\n\n"
        )
        result[names[path] + ".md"] = notice + text
    result["_Sidebar.md"] = make_sidebar(source, names, texts)
    return result


def make_sidebar(source: Path, names: dict[Path, str], texts: dict[Path, str]) -> str:
    sidebar = [
        "[Home](Home)",
        "",
        "- [Quick start](QUICK_START)",
        "- [Airflow SQL manual](AIRFLOW_SQL_MANUAL)",
        "- [Modules](Modules)",
    ]
    module_text = texts[source / "modules/README.md"]
    for match in LINK.finditer(module_text):
        target = (source / "modules" / match.group("url")).resolve()
        if target in names and target.name == "index.md":
            label = match.group(0).split("]", 1)[0][1:]
            sidebar.append(f"- [{label}]({names[target]})")
            functions = target.parent / "functions/index.md"
            if functions in names:
                sidebar.append(f"  - [Function reference]({names[functions]})")
    sidebar.extend(
        ["- [Changelog](CHANGELOG)", "", "Generated from main; edit repository docs.", ""]
    )
    return "\n".join(sidebar)


def owned_pages(output: Path, pages: dict[str, str]) -> list[str]:
    """Validate every output path before any mutation."""
    manifest = output / MANIFEST
    if manifest.is_symlink():
        fail("Wiki manifest symlinks are unsupported")
    old = json.loads(manifest.read_text(encoding="utf-8")) if manifest.exists() else []
    if not isinstance(old, list) or any(
        not isinstance(name, str) or not re.fullmatch(r"[\w-]+\.md", name) for name in old
    ):
        fail("Invalid wiki ownership manifest")
    if output.is_symlink() or any((output / name).is_symlink() for name in set(old) | pages.keys()):
        fail("Wiki output symlinks are unsupported")
    for name in set(old) | pages.keys():
        if (output / name).exists() and not (output / name).is_file():
            fail(f"Wiki output is not a file: {name}")
    for name in pages:
        if name not in old and (output / name).exists() and name != "Home.md":
            fail(f"Refusing to overwrite an unowned wiki page: {name}")
    return old


def sync_pages(output: Path, pages: dict[str, str]) -> bool:
    """Replace only owned files; validate ownership before writing anything."""
    old = owned_pages(output, pages)
    output.mkdir(parents=True, exist_ok=True)
    changed = False
    for name in set(old) - pages.keys():
        if (output / name).exists():
            (output / name).unlink()
            changed = True
    values = {**pages, MANIFEST: json.dumps(sorted(pages), indent=2) + "\n"}
    for name, content in values.items():
        target = output / name
        if not target.exists() or target.read_text(encoding="utf-8") != content:
            target.write_text(content, encoding="utf-8")
            changed = True
    return changed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-directory", type=Path, default=Path("docs"))
    parser.add_argument("--output-directory", type=Path)
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    if not args.check_only and args.output_directory is None:
        parser.error("--output-directory is required unless --check-only is used")
    try:
        if not args.check_only:
            source = args.source_directory.resolve()
            output = args.output_directory.resolve()
            if source == output or source in output.parents or output in source.parents:
                fail("Output must be separate from source documentation")
        pages = export_pages(args.source_directory)
        if args.check_only:
            print(f"Validated {len(pages)} wiki pages")
        else:
            changed = sync_pages(args.output_directory, pages)
            print("Wiki updated" if changed else "Wiki already current")
    except (ValueError, OSError) as exc:
        parser.exit(1, f"Wiki export failed: {exc}\n")


if __name__ == "__main__":
    main()
