import os
import sys
import asyncio
import asyncpg
from typing import Dict, List, Any, Optional, Set
from embedder_config import load_active_profile

DATABASE_URL = os.environ.get("COCOINDEX_DATABASE_URL")
PG_SCHEMA = "public"

# Commonly used external packages in Python, JavaScript/TypeScript, and C#
EXTERNAL_PACKAGES = {
    # Python stdlib & common packages
    "os", "sys", "re", "time", "asyncio", "json", "hashlib", "pathlib", "dataclasses", "typing", "collections",
    "numpy", "pandas", "asyncpg", "streamlit", "sentence_transformers", "langchain", "langchain_core", 
    "langchain_ollama", "transformers", "huggingface", "logging", "math", "socket", "struct", "traceback",
    # JS/TS node stdlib & common npm
    "fs", "path", "crypto", "events", "util", "stream", "http", "https", "express", "react", "next", "lodash",
    # C# System namespaces
    "System", "System.Collections", "System.Collections.Generic", "System.Linq", "System.Text", "System.Threading",
    "System.Threading.Tasks", "System.IO", "Microsoft", "Newtonsoft", "Newtonsoft.Json"
}

def get_edge_table_name(table_name: str) -> str:
    return f"{table_name}_graph_edges"

def _normalize_simple_name(symbol: str) -> str:
    raw = (symbol or "").strip()
    if not raw:
        return ""
    raw = raw.replace("new ", "")
    raw = raw.split("(")[0].strip()
    raw = raw.split(" as ")[0].strip()
    raw = raw.split(".")[-1].strip()
    raw = raw.split("::")[-1].strip()
    return raw

async def resolve_unresolved_edges() -> Dict[str, int]:
    """
    Main entry point for resolving unresolved/ambiguous edges in the database.
    """
    if not DATABASE_URL:
        print("[LINKER] COCOINDEX_DATABASE_URL is not set.")
        return {"updated": 0}

    prof = load_active_profile()
    table_name = prof.table_name
    edge_table = get_edge_table_name(table_name)

    print(f"[LINKER] Starting symbol resolution on tables: {table_name} and {edge_table}")

    async with await asyncpg.create_pool(DATABASE_URL) as pool:
        async with pool.acquire() as conn:
            # 1. Check if tables exist
            table_check = await conn.fetchval(
                "SELECT EXISTS (SELECT FROM information_schema.tables WHERE table_schema = $1 AND table_name = $2)",
                PG_SCHEMA, table_name
            )
            edge_check = await conn.fetchval(
                "SELECT EXISTS (SELECT FROM information_schema.tables WHERE table_schema = $1 AND table_name = $2)",
                PG_SCHEMA, edge_table
            )

            if not table_check or not edge_check:
                print(f"[LINKER] Missing nodes or edges table. Nodes exist: {table_check}, Edges exist: {edge_check}")
                return {"updated": 0}

            # 2. Fetch all nodes
            print("[LINKER] Fetching graph nodes...")
            node_rows = await conn.fetch(
                f'SELECT puid, filename, repo_name, node_type, node_name, qualified_name, parent_puid, is_skeleton FROM "{PG_SCHEMA}"."{table_name}"'
            )

            # Build in-memory indexes
            nodes_by_puid: Dict[str, Dict[str, Any]] = {}
            nodes_by_name: Dict[str, List[Dict[str, Any]]] = {}
            nodes_by_qualified_name: Dict[str, List[Dict[str, Any]]] = {}
            file_nodes: List[Dict[str, Any]] = []

            for r in node_rows:
                node = dict(r)
                puid = node["puid"]
                nodes_by_puid[puid] = node
                
                # Index by node_name
                name = node["node_name"]
                if name:
                    nodes_by_name.setdefault(name, []).append(node)
                
                # Index by qualified_name
                qname = node["qualified_name"]
                if qname:
                    nodes_by_qualified_name.setdefault(qname, []).append(node)

                # Collect file nodes
                if node["node_type"] == "file":
                    file_nodes.append(node)

            print(f"[LINKER] Indexed {len(nodes_by_puid)} nodes ({len(file_nodes)} file nodes).")

            # 3. Fetch all edges
            print("[LINKER] Fetching edges...")
            edge_rows = await conn.fetch(
                f'SELECT id, source_puid, target_puid, edge_type, resolution_status, confidence, source_symbol, target_symbol, source_line, target_line, metadata, repo_name, filename, lang FROM "{PG_SCHEMA}"."{edge_table}"'
            )
            edges: List[Dict[str, Any]] = [dict(r) for r in edge_rows]
            print(f"[LINKER] Loaded {len(edges)} total edges.")

            # Build imports index per file
            # key: file_puid, value: list of target_symbols imported
            imports_by_file: Dict[str, List[Dict[str, Any]]] = {}
            for edge in edges:
                if edge["edge_type"] == "imports":
                    src = edge["source_puid"]
                    imports_by_file.setdefault(src, []).append(edge)

            # 4. Resolve imports first (Task 3.2)
            resolved_imports_count = 0
            resolved_calls_count = 0
            updates: List[tuple] = []

            for edge in edges:
                edge_id = edge["id"]
                edge_type = edge["edge_type"]
                status = edge["resolution_status"]
                target_symbol = edge["target_symbol"] or ""
                source_puid = edge["source_puid"]

                if edge_type == "imports" and status in ("unresolved", "ambiguous"):
                    # Attempt to resolve import target symbol to a file node
                    resolved_puid = ""
                    # 1. Exact match on qualified_name/path
                    matched_files = [f for f in file_nodes if f["qualified_name"] == target_symbol or f["filename"].endswith(target_symbol)]
                    
                    if not matched_files:
                        # 2. Match module name (e.g. "ast_chunker" matches "ast_chunker.py")
                        clean_sym = target_symbol.split(".")[-1]
                        matched_files = [f for f in file_nodes if f["node_name"].split(".")[0] == clean_sym or f["qualified_name"].split("/")[-1].split(".")[0] == clean_sym]

                    if len(matched_files) == 1:
                        resolved_puid = matched_files[0]["puid"]
                        new_status = "resolved"
                        new_conf = 0.95
                        resolved_imports_count += 1
                    elif len(matched_files) > 1:
                        new_status = "ambiguous"
                        new_conf = 0.45
                    else:
                        # Check if it is an external package
                        base_package = target_symbol.split(".")[0].split("/")[0]
                        if base_package in EXTERNAL_PACKAGES:
                            new_status = "external"
                            new_conf = 0.75
                        else:
                            new_status = "unresolved"
                            new_conf = 0.3

                    updates.append((resolved_puid, new_status, new_conf, edge_id))

            # Apply imports updates to database first so call resolution can refer to resolved imports
            if updates:
                await conn.executemany(
                    f'UPDATE "{PG_SCHEMA}"."{edge_table}" SET target_puid = $1, resolution_status = $2, confidence = $3 WHERE id = $4',
                    updates
                )
                print(f"[LINKER] Resolved {resolved_imports_count} import edges.")
                # Reload updated imports for call linker
                edge_rows = await conn.fetch(
                    f'SELECT id, source_puid, target_puid, edge_type, resolution_status, confidence, source_symbol, target_symbol, source_line, target_line, metadata, repo_name, filename, lang FROM "{PG_SCHEMA}"."{edge_table}"'
                )
                edges = [dict(r) for r in edge_rows]
                imports_by_file.clear()
                for edge in edges:
                    if edge["edge_type"] == "imports":
                        src = edge["source_puid"]
                        imports_by_file.setdefault(src, []).append(edge)

            # 5. Resolve calls, inherits, implements (Task 3.3)
            updates.clear()
            for edge in edges:
                edge_id = edge["id"]
                edge_type = edge["edge_type"]
                status = edge["resolution_status"]
                target_symbol = edge["target_symbol"] or ""
                source_puid = edge["source_puid"]
                lang = (edge["lang"] or "").lower()

                if edge_type in ("calls", "inherits", "implements") and status in ("unresolved", "ambiguous"):
                    resolved_puid = ""
                    new_status = "unresolved"
                    new_conf = 0.3

                    # Get source file node
                    src_node = nodes_by_puid.get(source_puid)
                    if not src_node:
                        continue

                    # Determine containing file node PUID
                    file_puid = ""
                    if src_node["node_type"] == "file":
                        file_puid = source_puid
                    else:
                        # Backtrack to parent of kind file
                        curr = src_node
                        while curr and curr.get("parent_puid"):
                            parent = nodes_by_puid.get(curr["parent_puid"])
                            if parent and parent["node_type"] == "file":
                                file_puid = parent["puid"]
                                break
                            curr = parent

                    # Get clean/simple target name
                    simple_target = _normalize_simple_name(target_symbol)
                    if not simple_target:
                        continue

                    # Candidate resolution strategies:
                    # Strategy A: Check if symbol is defined locally in the same file
                    local_candidates = []
                    if file_puid:
                        # Find nodes whose parent_puid is eventually tracing back to file_puid
                        for node_puid, node in nodes_by_puid.items():
                            if node["node_name"] == simple_target:
                                # Trace back to see if it belongs to file_puid
                                is_local = False
                                curr = node
                                while curr:
                                    if curr["puid"] == file_puid:
                                        is_local = True
                                        break
                                    curr = nodes_by_puid.get(curr.get("parent_puid", ""))
                                if is_local:
                                    local_candidates.append(node)

                    if len(local_candidates) == 1:
                        resolved_puid = local_candidates[0]["puid"]
                        new_status = "resolved"
                        new_conf = 0.95
                        resolved_calls_count += 1

                    # Strategy B: Trace imports of the source file
                    if not resolved_puid and file_puid:
                        file_imports = imports_by_file.get(file_puid, [])
                        for imp in file_imports:
                            imp_target = imp["target_symbol"] or ""
                            # If we imported the target symbol explicitly (e.g. from x import target)
                            # or imported module x containing target
                            imp_file_puid = imp["target_puid"]
                            if imp_file_puid:
                                # Search for definition of simple_target in the imported file
                                imported_nodes = [
                                    n for n in nodes_by_puid.values()
                                    if n["node_name"] == simple_target and (
                                        n["puid"] == imp_file_puid or 
                                        n["parent_puid"] == imp_file_puid or
                                        # trace back to imported file
                                        any(
                                            parent["puid"] == imp_file_puid
                                            for parent in [nodes_by_puid.get(n.get("parent_puid", ""))]
                                            if parent
                                        )
                                    )
                                ]
                                if len(imported_nodes) == 1:
                                    resolved_puid = imported_nodes[0]["puid"]
                                    new_status = "resolved"
                                    new_conf = 0.90
                                    resolved_calls_count += 1
                                    break

                    # Strategy C: Global fallback - check if symbol is unique in the entire repository/workspace
                    if not resolved_puid:
                        global_candidates = nodes_by_name.get(simple_target, [])
                        # Filter to only look in same repo_name
                        repo_candidates = [n for n in global_candidates if n["repo_name"] == edge["repo_name"] and not n["is_skeleton"]]
                        
                        if len(repo_candidates) == 1:
                            resolved_puid = repo_candidates[0]["puid"]
                            new_status = "resolved"
                            new_conf = 0.85
                            resolved_calls_count += 1
                        elif len(repo_candidates) > 1:
                            new_status = "ambiguous"
                            new_conf = 0.45
                        else:
                            # Strategy D: Check if it's an external library
                            base_package = target_symbol.split(".")[0].split("/")[0]
                            if base_package in EXTERNAL_PACKAGES:
                                new_status = "external"
                                new_conf = 0.70
                            else:
                                new_status = "unresolved"
                                new_conf = 0.3

                    updates.append((resolved_puid, new_status, new_conf, edge_id))

            if updates:
                await conn.executemany(
                    f'UPDATE "{PG_SCHEMA}"."{edge_table}" SET target_puid = $1, resolution_status = $2, confidence = $3 WHERE id = $4',
                    updates
                )
                print(f"[LINKER] Resolved {resolved_calls_count} calls/inherits/implements edges.")

            return {"updated": resolved_imports_count + resolved_calls_count}

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    result = loop.run_until_complete(resolve_unresolved_edges())
    print(f"[LINKER] Complete. Total edges updated/resolved: {result['updated']}")
