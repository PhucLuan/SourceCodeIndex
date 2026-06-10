import asyncio
import os
import sys
from pathlib import Path
import asyncpg

ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(ROOT))

from embedder_config import load_active_profile
from graph_symbol_linker import resolve_unresolved_edges
from rag import get_neighbors, get_incoming_edges, get_outgoing_edges, get_shortest_path, lookup_symbol

DATABASE_URL = os.environ.get("COCOINDEX_DATABASE_URL")
PG_SCHEMA = "public"

async def main() -> int:
    if not DATABASE_URL:
        print("COCOINDEX_DATABASE_URL environment variable is not set.")
        return 1

    print("Phase 3 testing starting...")
    print("---------------------------")
    
    # 1. Trigger the symbol resolution
    print("[TEST] Running symbol resolution linker...")
    res = await resolve_unresolved_edges()
    print(f"[TEST] Linker resolved/updated: {res['updated']} edges.")

    prof = load_active_profile()
    edge_table = f"{prof.table_name}_graph_edges"

    # 2. Check resolution statistics in database
    async with await asyncpg.create_pool(DATABASE_URL) as pool:
        async with pool.acquire() as conn:
            counts = await conn.fetch(
                f'SELECT resolution_status, COUNT(*) as cnt FROM "{PG_SCHEMA}"."{edge_table}" GROUP BY resolution_status'
            )
            print("\nEdge Resolution Status Summary in DB:")
            print("-------------------------------------")
            status_map = {}
            for r in counts:
                print(f"- {r['resolution_status']}: {r['cnt']}")
                status_map[r['resolution_status']] = r['cnt']

            # Make sure we have some 'resolved' edges
            resolved_count = status_map.get("resolved", 0)
            if resolved_count == 0:
                print("\n[ERROR] No resolved edges found in the database. Cross-file linking might have failed.")
                return 1
            else:
                print(f"\n[PASS] Found {resolved_count} resolved edges.")

            # Fetch one resolved edge to test helper functions
            test_edge = await conn.fetchrow(
                f'SELECT source_puid, target_puid FROM "{PG_SCHEMA}"."{edge_table}" WHERE resolution_status = \'resolved\' LIMIT 1'
            )
            
            if not test_edge:
                print("[ERROR] Could not fetch a test resolved edge.")
                return 1

            source_puid = test_edge["source_puid"]
            target_puid = test_edge["target_puid"]
            print(f"\n[TEST] Testing helpers with source: {source_puid} and target: {target_puid}")

            # Test get_neighbors
            neighbors = await get_neighbors(source_puid)
            print(f"- get_neighbors count: {len(neighbors)}")
            if len(neighbors) == 0:
                print("[ERROR] get_neighbors returned 0 neighbors.")
                return 1
            print("[PASS] get_neighbors is working.")

            # Test get_outgoing_edges
            outgoing = await get_outgoing_edges(source_puid)
            print(f"- get_outgoing_edges count: {len(outgoing)}")
            print("[PASS] get_outgoing_edges is working.")

            # Test get_incoming_edges
            incoming = await get_incoming_edges(target_puid)
            print(f"- get_incoming_edges count: {len(incoming)}")
            if len(incoming) == 0:
                print("[ERROR] get_incoming_edges returned 0 incoming edges for the target.")
                return 1
            print("[PASS] get_incoming_edges is working.")

            # Test get_shortest_path
            path = await get_shortest_path(source_puid, target_puid)
            print(f"- get_shortest_path length: {len(path)}")
            if len(path) == 0:
                print("[ERROR] get_shortest_path returned no path between connected nodes.")
                return 1
            print(f"- Path: {path}")
            print("[PASS] get_shortest_path is working.")

            # Test lookup_symbol (Task 3.1)
            print("\n[TEST] Testing lookup_symbol (Task 3.1)...")
            symbol_seed = await conn.fetchrow(
                f'''
                SELECT node_name, qualified_name, repo_name
                FROM "{PG_SCHEMA}"."{prof.table_name}"
                WHERE is_skeleton = FALSE
                  AND node_name IS NOT NULL
                  AND node_name <> ''
                ORDER BY CASE WHEN node_type IN ('function', 'method', 'class') THEN 0 ELSE 1 END,
                         char_length(COALESCE(qualified_name, node_name)) ASC
                LIMIT 1
                '''
            )

            if not symbol_seed:
                print("[ERROR] Could not find a symbol seed for lookup_symbol.")
                return 1

            exact_query = symbol_seed["qualified_name"] or symbol_seed["node_name"]
            exact_res = await lookup_symbol(exact_query, repo_name=symbol_seed["repo_name"])
            print(f"- Exact lookup('{exact_query}') count: {len(exact_res)}")
            if len(exact_res) == 0:
                print(f"[ERROR] Exact lookup failed to find '{exact_query}'.")
                return 1
            print(f"  Found: {exact_res[0]['puid']} ({exact_res[0]['node_name']})")
            print("[PASS] Exact lookup is working.")
            
            # Fuzzy lookup
            fuzzy_term = (symbol_seed["node_name"] or "")[:4] or "code"
            fuzzy_res = await lookup_symbol(fuzzy_term, repo_name=symbol_seed["repo_name"], fuzzy=True)
            print(f"- Fuzzy lookup('{fuzzy_term}') count: {len(fuzzy_res)}")
            if len(fuzzy_res) == 0:
                print(f"[ERROR] Fuzzy lookup failed to find similarity matches for '{fuzzy_term}'.")
                return 1
            print(f"  Top fuzzy match: {fuzzy_res[0]['node_name']} (score: {fuzzy_res[0]['score']:.4f})")
            print("[PASS] Fuzzy lookup is working.")


    print("\nPhase 3 checks passed successfully!")
    return 0

if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
