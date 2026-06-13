from graph_traversal import run_impact_bfs
from rag import lookup_symbol

def check_impact():
    print("Looking up AssetService...")
    seed_nodes = lookup_symbol("AssetService", fuzzy=True)
    start_puids = [n.get("puid", "") for n in seed_nodes[:3] if n.get("puid")]
    start_symbols = [n.get("node_name", "") for n in seed_nodes[:3] if n.get("node_name")]
    
    print(f"start_puids: {start_puids}")
    print(f"start_symbols: {start_symbols}")
    
    if not start_puids:
        print("No start PUIDs found.")
        return
        
    print("Running BFS...")
    result = run_impact_bfs(start_puids=start_puids, start_symbols=start_symbols, max_depth=3)
    
    affected = result.get("affected_nodes", [])
    print(f"Total affected: {len(affected)}")
    for node in affected[:15]:
        print(f" - {node.get('node_name')} (via {node.get('via_edge_type')}) depth: {node.get('depth')}")

if __name__ == '__main__':
    check_impact()
