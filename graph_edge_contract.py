from __future__ import annotations

from dataclasses import dataclass
import hashlib


GRAPH_EDGE_TYPES = (
    "imports",
    "exports",
    "calls",
    "inherits",
    "implements",
    "contains",
    "depends_on",
)

EDGE_RESOLUTION_STATUSES = (
    "resolved",
    "ambiguous",
    "external",
    "unresolved",
)


@dataclass(frozen=True)
class GraphEdge:
    id: str
    source_puid: str
    target_puid: str = ""
    edge_type: str = "depends_on"
    resolution_status: str = "unresolved"
    confidence: float = 1.0
    source_symbol: str = ""
    target_symbol: str = ""
    source_line: int = 0
    target_line: int = 0
    metadata: str = ""
    repo_name: str = ""
    filename: str = ""
    lang: str = ""


def canonicalize_edge_type(edge_type: str) -> str:
    raw = (edge_type or "").strip().lower()
    return raw if raw in GRAPH_EDGE_TYPES else "depends_on"


def canonicalize_resolution_status(status: str) -> str:
    raw = (status or "").strip().lower()
    return raw if raw in EDGE_RESOLUTION_STATUSES else "unresolved"


def make_edge_id(
    source_puid: str,
    target_puid: str,
    edge_type: str,
    target_symbol: str = "",
    source_line: int = 0,
    target_line: int = 0,
) -> str:
    payload = "|".join(
        [
            source_puid or "",
            target_puid or "",
            canonicalize_edge_type(edge_type),
            target_symbol or "",
            str(source_line or 0),
            str(target_line or 0),
        ]
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()
