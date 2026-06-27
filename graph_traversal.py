from __future__ import annotations

from collections import deque
from typing import Any, Optional

import asyncpg

from indexer_flow import DATABASE_URL, PG_SCHEMA, TABLE_NAME, fetch_nodes, get_graph_edge_table_name


DEFAULT_IMPACT_EDGE_TYPES = {"calls", "imports", "inherits", "implements"}
STRICT_MIN_CONFIDENCE = 0.80


def _fetch_all_edges_sync(
    edge_types: set[str],
    repo_name: str | None = None,
) -> list[dict[str, Any]]:
    async def _run() -> list[dict[str, Any]]:
        edge_table = get_graph_edge_table_name(TABLE_NAME)
        params: list[object] = [list(edge_types)]
        clauses = ["edge_type = ANY($1)"]
        if repo_name:
            params.append(repo_name)
            clauses.append(f"repo_name = ${len(params)}")

        query = f"""
            SELECT id, repo_name, filename, lang, edge_type, resolution_status, confidence,
                   source_puid, target_puid, source_symbol, target_symbol, source_line, target_line, metadata
            FROM "{PG_SCHEMA}"."{edge_table}"
            WHERE {" AND ".join(clauses)}
            ORDER BY edge_type, source_puid, target_puid, source_line
        """

        async with await asyncpg.create_pool(DATABASE_URL) as pool:
            rows = await pool.fetch(query, *params)
            return [dict(r) for r in rows]

    def _safe_run():
        import asyncio
        import threading
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            
        if loop.is_running():
            result = []
            def thread_target():
                new_loop = asyncio.new_event_loop()
                result.extend(new_loop.run_until_complete(_run()))
                new_loop.close()
            t = threading.Thread(target=thread_target)
            t.start()
            t.join()
            return result
        else:
            return loop.run_until_complete(_run())

    try:
        return _safe_run()
    except Exception as ex:
        import sys
        sys.stderr.write(f"[WARN] _fetch_all_edges_sync failed: {ex}\n")
        return []


def run_impact_bfs(
    start_puids: list[str],
    start_symbols: list[str] = None,
    edge_types: set[str] | None = None,
    max_depth: int = 3,
    repo_name: str | None = None,
    mode: str = "strict",
) -> dict[str, Any]:
    """Reverse BFS over the graph edges to find nodes affected by a change.

    mode="strict" (default): only traverse edges with resolution_status=="resolved"
    and confidence >= STRICT_MIN_CONFIDENCE. No substring/name fallback matching is
    used — every traversed hop must be backed by a resolved PUID-to-PUID edge.

    mode="exploratory": also considers ambiguous/unresolved edges and the legacy
    substring-based symbol fallback, but every such hop is flagged with
    is_strict=False so callers can route it into warnings instead of the trusted tree.
    """
    if edge_types is None:
        edge_types = set(DEFAULT_IMPACT_EDGE_TYPES)
        edge_types.add("contains")

    if not start_symbols:
        start_symbols = []

    start_puids = [p for p in start_puids if p]
    strict_mode = mode != "exploratory"
    if not start_puids and (strict_mode or not start_symbols):
        return {
            "affected_nodes": [],
            "edges": [],
            "max_depth_reached": False,
            "total_count": 0,
        }

    strict = strict_mode

    all_edges = _fetch_all_edges_sync(edge_types, repo_name)
    if strict:
        all_edges = [
            e
            for e in all_edges
            if (e.get("resolution_status") or "") == "resolved"
            and float(e.get("confidence") or 0.0) >= STRICT_MIN_CONFIDENCE
        ]

    reverse_adj: dict[str, list[dict[str, Any]]] = {}
    forward_adj: dict[str, list[dict[str, Any]]] = {}

    for edge in all_edges:
        tgt = edge.get("target_puid")
        src = edge.get("source_puid")

        if tgt:
            reverse_adj.setdefault(tgt, []).append(edge)
        elif not strict:
            tgt_sym = edge.get("target_symbol")
            if tgt_sym:
                for sym in start_symbols:
                    if sym and (sym.lower() in tgt_sym.lower()):
                        reverse_adj.setdefault(sym, []).append(edge)
                        break

        if src:
            forward_adj.setdefault(src, []).append(edge)

    queue = deque()
    for puid in start_puids:
        queue.append((puid, 0, [puid]))
    if not strict:
        for sym in start_symbols:
            if sym:
                queue.append((sym, 0, [sym]))

    visited: set[str] = set(start_puids)
    if not strict:
        visited.update(s for s in start_symbols if s)
    affected: list[dict[str, Any]] = []
    result_edges: list[dict[str, Any]] = []
    max_depth_reached = False

    def _node_ref(puid: str) -> dict[str, str]:
        node = node_lookup.get(puid, {})
        return {
            "puid": puid,
            "symbol": node.get("node_name") or node.get("qualified_name") or "",
            "file": node.get("filename") or "",
        }

    node_lookup: dict[str, dict[str, Any]] = {}
    all_puids = {p for p in (start_puids or [])}
    for edge in all_edges:
        for key in ("source_puid", "target_puid"):
            puid = edge.get(key)
            if puid:
                all_puids.add(puid)
    if all_puids:
        try:
            for node in fetch_nodes(list(all_puids)):
                node_lookup[node.get("puid", "")] = node
        except Exception as ex:
            import sys
            sys.stderr.write(f"[WARN] run_impact_bfs node lookup failed: {ex}\n")

    def _make_evidence_edge(edge: dict[str, Any], source_puid: str, target_puid: str, depth: int, path: list[str]) -> dict[str, Any]:
        source_node = node_lookup.get(source_puid, {})
        target_node = node_lookup.get(target_puid, {})
        return {
            "source_puid": source_puid,
            "source_symbol": source_node.get("node_name") or edge.get("source_symbol") or "",
            "source_file": source_node.get("filename") or edge.get("filename") or "",
            "source_line": edge.get("source_line") or "line_unknown",
            "target_puid": target_puid,
            "target_symbol": target_node.get("node_name") or edge.get("target_symbol") or "",
            "target_file": target_node.get("filename") or edge.get("filename") or "",
            "target_line": edge.get("target_line") or "line_unknown",
            "edge_type": edge.get("edge_type", ""),
            "resolution_status": edge.get("resolution_status", ""),
            "confidence": float(edge.get("confidence") or 0.0),
            "metadata": edge.get("metadata", ""),
            "depth": depth,
            "path": list(path),
        }

    while queue:
        node, depth, path = queue.popleft()
        if depth >= max_depth:
            max_depth_reached = True
            continue

        for edge in reverse_adj.get(node, []):
            caller = edge.get("source_puid")
            if not caller or caller in visited:
                continue

            visited.add(caller)
            next_path = path + [caller]
            ref = _node_ref(caller)
            affected.append(
                {
                    "puid": caller,
                    "node_name": ref["symbol"] or edge.get("source_symbol", ""),
                    "filename": ref["file"] or edge.get("filename", ""),
                    "depth": depth + 1,
                    "via_edge_type": edge.get("edge_type", ""),
                    "path": next_path,
                }
            )
            result_edges.append(_make_evidence_edge(edge, caller, node, depth + 1, next_path))
            queue.append((caller, depth + 1, next_path))

        for edge in forward_adj.get(node, []):
            if edge.get("edge_type") == "contains":
                child = edge.get("target_puid")
                if not child or child in visited:
                    continue
                visited.add(child)
                next_path = path + [child]
                ref = _node_ref(child)
                affected.append(
                    {
                        "puid": child,
                        "node_name": ref["symbol"] or edge.get("target_symbol", ""),
                        "filename": ref["file"] or edge.get("filename", ""),
                        "depth": depth + 1,
                        "via_edge_type": "contains",
                        "path": next_path,
                    }
                )
                result_edges.append(_make_evidence_edge(edge, node, child, depth + 1, next_path))
                queue.append((child, depth + 1, next_path))

    return {
        "affected_nodes": affected,
        "edges": result_edges,
        "max_depth_reached": max_depth_reached,
        "total_count": len(affected),
        "mode": mode,
    }


def build_impact_tree(
    seed_puids: list[str],
    affected_nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    seed_info: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Phase 5A.7 - Turn the flat reverse-BFS result into a nested ripple tree.

    Each affected node carries its root-to-node `path` (list of PUIDs) from
    run_impact_bfs(). We use those paths to build a parent->children trie
    rooted at the seed(s), so the LLM receives an explicit "oil-spill" tree:
    seed -> direct callers -> their callers -> ... instead of a flat list.
    """
    seed_info = seed_info or {}

    node_meta: dict[str, dict[str, Any]] = {}
    for puid in seed_puids:
        meta = seed_info.get(puid, {})
        node_meta[puid] = {
            "puid": puid,
            "symbol": meta.get("node_name") or meta.get("qualified_name") or "",
            "file": meta.get("filename") or "",
        }
    for node in affected_nodes:
        puid = node.get("puid")
        if not puid:
            continue
        node_meta[puid] = {
            "puid": puid,
            "symbol": node.get("node_name", ""),
            "file": node.get("filename", ""),
        }

    # edge evidence is recorded as source_puid=child(caller), target_puid=parent
    # (the node being called). Index by (parent, child) for quick lookup.
    edge_lookup: dict[tuple[str, str], dict[str, Any]] = {}
    for edge in edges:
        key = (edge.get("target_puid"), edge.get("source_puid"))
        edge_lookup[key] = edge

    children_order: dict[str, list[str]] = {}
    seen_pairs: set[tuple[str, str]] = set()
    for node in affected_nodes:
        path = node.get("path") or []
        for i in range(len(path) - 1):
            parent, child = path[i], path[i + 1]
            pair = (parent, child)
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            children_order.setdefault(parent, []).append(child)

    def build_node(puid: str) -> dict[str, Any]:
        meta = node_meta.get(puid, {"puid": puid, "symbol": "", "file": ""})
        node_dict = dict(meta)
        children = []
        for child in children_order.get(puid, []):
            edge = edge_lookup.get((puid, child), {})
            child_node = build_node(child)
            child_node["edge_type"] = edge.get("edge_type", "")
            child_node["line"] = edge.get("source_line", "line_unknown")
            child_node["resolution_status"] = edge.get("resolution_status", "")
            child_node["confidence"] = edge.get("confidence", 0.0)
            children.append(child_node)
        node_dict["children"] = children
        return node_dict

    return [build_node(seed) for seed in seed_puids]


def render_impact_tree_text(tree: list[dict[str, Any]], label: str = "CHANGED") -> str:
    """Render a nested impact tree (from build_impact_tree) as indented text for an LLM prompt."""
    lines: list[str] = []

    def walk(node: dict[str, Any], depth: int, prefix: str) -> None:
        indent = "  " * depth
        symbol = node.get("symbol") or node.get("puid", "")
        file_ref = node.get("file", "")
        if depth == 0:
            lines.append(f"{indent}[{label}] {symbol} ({file_ref})")
        else:
            edge_type = node.get("edge_type", "")
            line = node.get("line", "line_unknown")
            lines.append(f"{indent}{prefix} [{edge_type}] {symbol} ({file_ref}:L{line})")
        for child in node.get("children", []):
            walk(child, depth + 1, "affected by ->")

    for root in tree:
        walk(root, 0, "")
    return "\n".join(lines) if lines else "No impact tree could be built."


def impact_puids_to_nodes(impact_result: dict[str, Any]) -> list[dict[str, Any]]:
    affected = impact_result.get("affected_nodes", [])
    puids = [node.get("puid") for node in affected if node.get("puid")]
    if not puids:
        return []

    nodes = fetch_nodes(puids)
    depth_map = {node.get("puid", ""): node.get("depth", 0) for node in affected}
    for node in nodes:
        node["depth"] = depth_map.get(node.get("puid", ""), 0)
    nodes.sort(key=lambda x: depth_map.get(x.get("puid", ""), 99))
    return nodes
