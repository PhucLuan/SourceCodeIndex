from __future__ import annotations

from dataclasses import dataclass
import re


GRAPH_NODE_KINDS = (
    "file",
    "function",
    "class",
    "method",
    "module",
    "concept",
)

FILE_NODE_KIND = "file"
FUNCTION_NODE_KIND = "function"
CLASS_NODE_KIND = "class"
METHOD_NODE_KIND = "method"
MODULE_NODE_KIND = "module"
CONCEPT_NODE_KIND = "concept"

NODE_KIND_SET = set(GRAPH_NODE_KINDS)


@dataclass(frozen=True)
class GraphNodeContract:
    kind: str
    qualified_name: str
    signature: str = ""
    modifiers: str = ""
    source_span: str = ""
    export_status: str = "unknown"
    is_skeleton: bool = False


def is_valid_node_kind(kind: str) -> bool:
    return kind in NODE_KIND_SET


def canonicalize_node_kind(
    raw_kind: str,
    *,
    is_file_node: bool = False,
    is_skeleton: bool = False,
) -> str:
    """Map parser-specific node labels to the graph contract."""
    raw = (raw_kind or "").strip().lower()

    if is_file_node:
        return FILE_NODE_KIND
    if is_skeleton or raw.endswith("_skeleton"):
        return CONCEPT_NODE_KIND
    if raw in {"file", "module"}:
        return FILE_NODE_KIND if raw == "file" else MODULE_NODE_KIND
    if raw in {"function", "function_definition", "function_declaration", "arrow_function"}:
        return FUNCTION_NODE_KIND
    if raw in {"method", "method_definition", "method_declaration"}:
        return METHOD_NODE_KIND
    if raw in {"class", "class_definition", "class_declaration"}:
        return CLASS_NODE_KIND
    if raw in {"interface_declaration", "enum_declaration", "struct_declaration"}:
        return CLASS_NODE_KIND
    if raw in {"file_chunk"}:
        return CONCEPT_NODE_KIND

    return CONCEPT_NODE_KIND


def resolve_graph_node_kind(
    lang: str,
    raw_kind: str,
    *,
    current_parent_type: str | None = None,
    is_file_node: bool = False,
    is_skeleton: bool = False,
) -> str:
    """Resolve parser-specific kinds into the shared graph contract."""
    kind = canonicalize_node_kind(
        raw_kind,
        is_file_node=is_file_node,
        is_skeleton=is_skeleton,
    )
    if (
        (lang or "").lower() == "python"
        and (raw_kind or "").strip().lower() == "function_definition"
        and (current_parent_type or "").strip().lower() == "class"
    ):
        return METHOD_NODE_KIND
    return kind


_MODIFIER_PATTERNS = (
    "export",
    "public",
    "private",
    "protected",
    "internal",
    "static",
    "abstract",
    "virtual",
    "override",
    "async",
    "readonly",
    "sealed",
    "partial",
    "final",
    "const",
)


def extract_modifiers(node_text: str, lang: str = "") -> str:
    """Best-effort modifier extraction shared across languages."""
    text = (node_text or "").strip()
    if not text:
        return ""

    head = text.splitlines()[0]
    found: list[str] = []
    head_lower = head.lower()
    for token in _MODIFIER_PATTERNS:
        if re.search(rf"\b{re.escape(token)}\b", head_lower):
            found.append(token)

    if lang.lower() in {"python"}:
        for token in ("async", "public", "private"):
            if token not in found and re.search(rf"\b{token}\b", head_lower):
                found.append(token)

    seen: set[str] = set()
    ordered = []
    for token in found:
        if token not in seen:
            seen.add(token)
            ordered.append(token)
    return ", ".join(ordered)
