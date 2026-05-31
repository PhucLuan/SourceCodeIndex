from __future__ import annotations

from typing import Callable

from graph_node_contract import extract_modifiers


def _decode(source_bytes: bytes, start_byte: int, end_byte: int) -> str:
    return source_bytes[start_byte:end_byte].decode("utf-8")


def extract_node_name(node, source_bytes: bytes) -> str:
    """Generic node-name resolver with small language-specific fallbacks."""
    for field_name in ("name", "identifier"):
        name_node = node.child_by_field_name(field_name)
        if name_node:
            return _decode(source_bytes, name_node.start_byte, name_node.end_byte)

    if node.type == "arrow_function":
        parent = node.parent
        if parent and parent.type == "variable_declarator":
            id_node = parent.child_by_field_name("name")
            if id_node:
                return _decode(source_bytes, id_node.start_byte, id_node.end_byte)

    if node.type == "element":
        start_tag = node.child_by_field_name("start_tag")
        if start_tag:
            tag_name_node = start_tag.child_by_field_name("name")
            if tag_name_node:
                return f"<{_decode(source_bytes, tag_name_node.start_byte, tag_name_node.end_byte)}>"
        return "<html>"

    if node.type == "rule_set":
        selectors = node.child_by_field_name("selectors")
        if selectors:
            return _decode(source_bytes, selectors.start_byte, selectors.end_byte).strip()
        return "css_rule"

    for child in node.children:
        if child.type == "identifier":
            return _decode(source_bytes, child.start_byte, child.end_byte)

    return "unknown"


def extract_signature(node, source_bytes: bytes) -> str:
    """Return the first line of the node as a lightweight signature."""
    text = _decode(source_bytes, node.start_byte, node.end_byte)
    return text.split("\n")[0].strip()


def extract_python_docstring(node, source_bytes: bytes) -> str:
    """Extract the leading Python docstring if one is present."""
    body = node.child_by_field_name("body")
    if not body:
        return ""

    for child in body.children:
        if child.type in {"newline", "comment"}:
            continue
        if child.type == "expression_statement" and child.children:
            first = child.children[0]
            if first.type in {"string", "string_content"}:
                raw = _decode(source_bytes, first.start_byte, first.end_byte).strip()
                return raw.strip('\"\'')
        break
    return ""


def extract_generic_docstring(node, source_bytes: bytes) -> str:
    """Best-effort docstring/comment summary for non-Python languages."""
    body = node.child_by_field_name("body")
    if body:
        text = _decode(source_bytes, body.start_byte, body.end_byte).strip()
        if text:
            first_line = text.split("\n")[0].strip()
            if first_line and len(first_line) <= 200:
                return first_line
    return ""


_DOCSTRING_EXTRACTORS: dict[str, Callable[[object, bytes], str]] = {
    "python": extract_python_docstring,
}


def register_docstring_extractor(lang: str, extractor: Callable[[object, bytes], str]) -> None:
    """Register a language-specific docstring extractor without touching the dispatcher."""
    _DOCSTRING_EXTRACTORS[(lang or "").lower()] = extractor


def extract_docstring(node, source_bytes: bytes, lang: str) -> str:
    extractor = _DOCSTRING_EXTRACTORS.get((lang or "").lower(), extract_generic_docstring)
    return extractor(node, source_bytes)


def build_modifiers(node_text: str, lang: str, node_type: str, export_status: str) -> str:
    """Shared modifier builder; language-specific behavior is encoded by text heuristics."""
    modifiers = extract_modifiers(node_text, lang)
    tokens = [m.strip() for m in modifiers.split(",") if m.strip()]
    if export_status == "exported" and "export" not in tokens:
        tokens.insert(0, "export")
    if node_type == "method" and "public" not in tokens and "private" not in tokens:
        # Access visibility is only added when explicitly expressed.
        pass
    seen: set[str] = set()
    ordered: list[str] = []
    for token in tokens:
        if token not in seen:
            seen.add(token)
            ordered.append(token)
    return ", ".join(ordered)
