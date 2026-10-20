"""Represent existing terminal underline spans as export-only SVG strokes.

This deliberately supports generated Rich SVG, not arbitrary browser CSS. Ambiguous
underlined geometry is an error rather than a silently missing decoration.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any
from xml.etree import ElementTree as ET

_NAMESPACE = "http://www.w3.org/2000/svg"
_PROPERTIES = (
    "fill",
    "color",
    "font-size",
    "text-decoration",
    "text-decoration-line",
    "text-decoration-color",
    "text-anchor",
    "fill-opacity",
    "writing-mode",
    "direction",
    "transform-origin",
)
_DECORATIONS = {"none", "underline", "line-through", "overline"}
_NUMBER = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")


def _declarations(value: str) -> dict[str, str]:
    result = {}
    for item in value.split(";"):
        key, separator, content = item.partition(":")
        if separator:
            result[key.strip()] = content.strip()
    return result


def _class_rules(root: ET.Element) -> list[tuple[str, dict[str, str]]]:
    rules: list[tuple[str, dict[str, str]]] = []
    for node in root.iter():
        if node.tag.rsplit("}", 1)[-1] != "style":
            continue
        css = re.sub(r"@font-face\s*\{[^{}]*\}", "", node.text or "")
        for raw_selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
            selector = raw_selector.strip()
            declarations = _declarations(body)
            if not re.fullmatch(r"\.[\w-]+", selector):
                if "underline" in body or any(key in declarations for key in _PROPERTIES):
                    message = "unsupported_underlined_css_selector"
                    raise ValueError(message)
                continue
            rules.append((selector[1:], declarations))
    return rules


def _local_style(node: ET.Element, rules: list[tuple[str, dict[str, str]]]) -> dict[str, str]:
    style = {key: node.attrib[key] for key in _PROPERTIES if key in node.attrib}
    classes = node.get("class", "").split()
    for name, declarations in rules:
        if name in classes:
            style.update(declarations)
    style.update(_declarations(node.get("style", "")))
    return style


def _number(value: str, *, pixels: bool = False) -> float:
    value = value[:-2] if pixels and value.endswith("px") else value
    if not _NUMBER.fullmatch(value):
        message = "unsupported_underline_length"
        raise ValueError(message)
    number = float(value)
    if not math.isfinite(number):
        message = "nonfinite_underline_length"
        raise ValueError(message)
    return number


def _decoration(style: dict[str, str]) -> set[str]:
    value = style.get("text-decoration-line", style.get("text-decoration", "none"))
    tokens = set(value.split()) - {"inherit"}
    if not tokens <= _DECORATIONS:
        message = "unsupported_underline_decoration"
        raise ValueError(message)
    return tokens - {"none"}


def _stroke(
    node: ET.Element,
    style: dict[str, str],
    chain: list[ET.Element],
    rules: list[tuple[str, dict[str, str]]],
) -> tuple[ET.Element, dict[str, Any]]:
    if any(
        key in ancestor.attrib
        for ancestor in chain
        for key in ("dx", "dy", "rotate", "mask", "filter")
    ):
        message = "unsupported_relative_underline_position"
        raise ValueError(message)
    x, baseline, width = (_number(node.attrib[key]) for key in ("x", "y", "textLength"))
    size = _number(style["font-size"], pixels=True)
    if width <= 0 or size <= 0 or style.get("text-anchor", "start") != "start":
        message = "ambiguous_underline_geometry"
        raise ValueError(message)
    if (
        style.get("writing-mode", "horizontal-tb") != "horizontal-tb"
        or style.get("direction", "ltr") != "ltr"
        or "transform-origin" in style
    ):
        message = "unsupported_underline_text_direction_or_origin"
        raise ValueError(message)
    color = style.get("text-decoration-color", style.get("fill", "black"))
    if color == "currentColor":
        color = style.get("color", "black")
    if color in {"inherit", "initial", "unset", "none"} or "var(" in color:
        message = "unresolved_underline_color"
        raise ValueError(message)
    y, thickness = baseline + size * 0.1, size * 0.1
    line = ET.Element(
        f"{{{_NAMESPACE}}}line",
        {
            "x1": str(x),
            "x2": str(x + width),
            "y1": str(y),
            "y2": str(y),
            "stroke": color,
            "stroke-width": str(thickness),
            "stroke-linecap": "butt",
            "stroke-opacity": style.get("fill-opacity", "1"),
            "fill": "none",
            "data-terminal-underline": "1",
        },
    )
    # Recreate only the text ancestry's geometric context. The surrounding
    # terminal groups/viewport remain in place and are applied exactly once.
    result = line
    contexts = []
    for ancestor in reversed(chain):
        local = _local_style(ancestor, rules)
        attributes = {
            key: local.get(key, ancestor.get(key, ""))
            for key in ("transform", "clip-path", "opacity", "visibility", "display")
            if key in local or key in ancestor.attrib
        }
        contexts.append(attributes)
        if attributes:
            wrapper = ET.Element(f"{{{_NAMESPACE}}}g", attributes)
            wrapper.append(result)
            result = wrapper
    return result, {
        "x": x,
        "baseline": baseline,
        "width": width,
        "font_size": size,
        "y": y,
        "thickness": thickness,
        "color": color,
        "contexts": [
            {key: value for key, value in context.items() if key in {"transform", "clip-path"}}
            for context in reversed(contexts)
        ],
    }


@dataclass
class _Context:
    style: dict[str, str] = field(default_factory=dict)
    decorations: set[str] = field(default_factory=set)
    terminal: bool = False
    text_root: ET.Element | None = None
    text_parent: ET.Element | None = None
    chain: list[ET.Element] = field(default_factory=list)


def _remove_underline(declarations: dict[str, str]) -> dict[str, str]:
    cleaned = dict(declarations)
    for key in ("text-decoration", "text-decoration-line"):
        if "underline" in cleaned.get(key, "").split():
            remaining = [
                decoration for decoration in cleaned[key].split() if decoration != "underline"
            ]
            cleaned[key] = " ".join(remaining) or "none"
    return cleaned


def _serialize_declarations(declarations: dict[str, str]) -> str:
    return ";".join(f"{key}:{value}" for key, value in declarations.items())


class _Normalizer:
    def __init__(self, root: ET.Element) -> None:
        self.root = root
        self.rules = _class_rules(root)
        self.records: list[dict[str, Any]] = []
        self.neutralize: list[tuple[ET.Element, set[str]]] = []
        self.additions: list[tuple[ET.Element, ET.Element, ET.Element]] = []

    def visit(self, node: ET.Element, parent: ET.Element | None, context: _Context) -> None:
        local = _local_style(node, self.rules)
        style = dict(context.style)
        style.update({key: value for key, value in local.items() if value != "inherit"})
        terminal = context.terminal or any(
            name.endswith("-matrix") for name in node.get("class", "").split()
        )
        if terminal and not context.terminal and "underline" in context.decorations:
            message = "underline_origin_outside_terminal"
            raise ValueError(message)
        own = _decoration(local)
        effective = context.decorations | own
        if terminal and "underline" in own:
            self.neutralize.append((node, own - {"underline"}))
        text_root, text_parent, chain = context.text_root, context.text_parent, context.chain
        if node.tag.rsplit("}", 1)[-1] == "text":
            text_root, text_parent, chain = node, parent, []
        if text_root is not None:
            chain = [*chain, node]
        current = _Context(style, effective, terminal, text_root, text_parent, chain)
        if terminal and text_root is not None and "underline" in effective:
            self.collect_span(node, current)
        for child in node:
            self.visit(child, node, current)

    def collect_span(self, node: ET.Element, context: _Context) -> None:
        if len(node):
            if node.text or any(child.tail for child in node):
                message = "ambiguous_mixed_underline_text"
                raise ValueError(message)
            return
        if node.tag.rsplit("}", 1)[-1] not in {"text", "tspan"} or context.text_parent is None:
            message = "unsupported_underlined_terminal_node"
            raise ValueError(message)
        if not node.text:
            message = "empty_underlined_terminal_span"
            raise ValueError(message)
        try:
            line, record = _stroke(node, context.style, context.chain, self.rules)
        except KeyError as exc:
            message = "missing_underline_geometry_or_style"
            raise ValueError(message) from exc
        if context.text_root is None:
            message = "missing_underline_text_root"
            raise ValueError(message)
        self.records.append(record)
        self.additions.append((context.text_parent, context.text_root, line))

    def _terminal_classes(self) -> dict[str, str]:
        # Clone all rules of affected classes in their original cascade order.
        # The original classes remain untouched for titles/outside-terminal text.
        names = {
            name
            for name, declarations in self.rules
            if _remove_underline(declarations) != declarations
        }
        used = {name for name, _declarations in self.rules}
        used.update(name for node in self.root.iter() for name in node.get("class", "").split())
        replacements = {}
        for name in sorted(names):
            candidate = name + "-terminal-geometry"
            while candidate in used:
                candidate += "-copy"
            used.add(candidate)
            replacements[name] = candidate
        for node in self.root.iter():
            if node.tag.rsplit("}", 1)[-1] == "style" and node.text:
                node.text = re.sub(
                    r"([^{}]+)\{([^{}]*)\}",
                    lambda match: self._clone_rule(match, replacements),
                    node.text,
                )
        return replacements

    @staticmethod
    def _clone_rule(match: re.Match[str], replacements: dict[str, str]) -> str:
        selector, body = match.groups()
        name = selector.strip()[1:]
        if selector.strip() != "." + name or name not in replacements:
            return match.group(0)
        cleaned = _serialize_declarations(_remove_underline(_declarations(body)))
        return match.group(0) + f"\n.{replacements[name]} {{{cleaned}}}"

    def apply(self) -> None:
        replacements = self._terminal_classes()
        for node, _remaining in self.neutralize:
            # Remove each declaration at its origin. An inline override does not
            # reliably cancel native decoration propagated by an ancestor.
            for key, value in _remove_underline(node.attrib).items():
                if key in ("text-decoration", "text-decoration-line"):
                    node.set(key, value)
            if "style" in node.attrib:
                node.set(
                    "style",
                    _serialize_declarations(_remove_underline(_declarations(node.attrib["style"]))),
                )
            classes = node.get("class", "").split()
            if any(name in replacements for name in classes):
                node.set("class", " ".join(replacements.get(name, name) for name in classes))
        for parent, text, line in reversed(self.additions):
            parent.insert(list(parent).index(text) + 1, line)


def normalize_terminal_underlines(svg: str) -> tuple[str, list[dict[str, Any]]]:
    """Return a normalized copy and conversion records; no work means exact bytes."""
    root = ET.fromstring(svg)  # noqa: S314 - generated terminal exports only.
    normalizer = _Normalizer(root)
    normalizer.visit(root, None, _Context())
    if not normalizer.records:
        return svg, []
    if any(
        node.get("clipPathUnits") == "objectBoundingBox"
        for node in root.iter()
        if node.tag.rsplit("}", 1)[-1] == "clipPath"
    ):
        message = "unsupported_object_bounding_box_underline_clip"
        raise ValueError(message)
    normalizer.apply()
    return ET.tostring(root, encoding="unicode"), normalizer.records
