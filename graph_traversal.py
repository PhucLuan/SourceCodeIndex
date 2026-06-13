from __future__ import annotations

from collections import deque
from typing import Any, Optional

import asyncpg

from indexer_flow import DATABASE_URL, PG_SCHEMA, TABLE_NAME, fetch_nodes, get_graph_edge_table_name


DEFAULT_IMPACT_EDGE_TYPES = {"calls", "imports", "inherits", "implements"}


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
) -> dict[str, Any]:
    if edge_types is None:
        edge_types = set(DEFAULT_IMPACT_EDGE_TYPES)
        edge_types.add("contains")
        
    if not start_symbols:
        start_symbols = []

    start_puids = [p for p in start_puids if p]
    if not start_puids:
        return {
            "affected_nodes": [],
            "edges": [],
            "max_depth_reached": False,
            "total_count": 0,
        }

    all_edges = _fetch_all_edges_sync(edge_types, repo_name)

    reverse_adj: dict[str, list[dict[str, Any]]] = {}
    forward_adj: dict[str, list[dict[str, Any]]] = {}
    
    for edge in all_edges:
        tgt = edge.get("target_puid")
        src = edge.get("source_puid")
        
        if tgt:
            reverse_adj.setdefault(tgt, []).append(edge)
        else:
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
    for sym in start_symbols:
        if sym:
            queue.append((sym, 0, [sym]))
            
    visited: set[str] = set(start_puids + [s for s in start_symbols if s])
    affected: list[dict[str, Any]] = []
    result_edges: list[tuple[str, str, str, int]] = []
    max_depth_reached = False

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
            affected.append(
                {
                    "puid": caller,
                    "node_name": edge.get("source_symbol", ""),
                    "filename": edge.get("filename", ""),
                    "depth": depth + 1,
                    "via_edge_type": edge.get("edge_type", ""),
                    "path": next_path,
                }
            )
            result_edges.append((caller, node, edge.get("edge_type", ""), depth + 1))
            queue.append((caller, depth + 1, next_path))

        for edge in forward_adj.get(node, []):
            if edge.get("edge_type") == "contains":
                child = edge.get("target_puid")
                if not child or child in visited:
                    continue
                visited.add(child)
                next_path = path + [child]
                affected.append(
                    {
                        "puid": child,
                        "node_name": edge.get("target_symbol", ""),
                        "filename": edge.get("filename", ""),
                        "depth": depth + 1,
                        "via_edge_type": "contains",
                        "path": next_path,
                    }
                )
                result_edges.append((node, child, "contains", depth + 1))
                queue.append((child, depth + 1, next_path))

    return {
        "affected_nodes": affected,
        "edges": result_edges,
        "max_depth_reached": max_depth_reached,
        "total_count": len(affected),
    }


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
