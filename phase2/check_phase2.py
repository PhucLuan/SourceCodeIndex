from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parent.parent
FILES = {
    "edge_contract": ROOT / "graph_edge_contract.py",
    "edge_extractor": ROOT / "graph_edge_extractor.py",
    "indexer": ROOT / "indexer_flow.py",
}


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def require(pattern: str, text: str, label: str, failures: list[str]) -> None:
    if not re.search(pattern, text, flags=re.MULTILINE):
        failures.append(f"missing {label}: /{pattern}/")


def main() -> int:
    failures: list[str] = []

    edge_contract_text = read_text(FILES["edge_contract"])
    edge_extractor_text = read_text(FILES["edge_extractor"])
    indexer_text = read_text(FILES["indexer"])

    require(r"GRAPH_EDGE_TYPES", edge_contract_text, "edge type contract", failures)
    for edge_type in ["imports", "exports", "calls", "inherits", "implements", "contains", "depends_on"]:
        require(rf"\b{edge_type}\b", edge_contract_text, f"edge type {edge_type}", failures)
    for status in ["resolved", "ambiguous", "external", "unresolved"]:
        require(rf"\b{status}\b", edge_contract_text, f"resolution status {status}", failures)
    require(r"class GraphEdge:", edge_contract_text, "GraphEdge dataclass", failures)
    require(r"def make_edge_id\(", edge_contract_text, "edge id helper", failures)

    require(r"def extract_graph_edges\(", edge_extractor_text, "graph edge extractor", failures)
    require(r"def _collect_contains\(", edge_extractor_text, "contains extractor", failures)
    require(r"def _collect_calls\(", edge_extractor_text, "call extractor", failures)
    require(r"def _collect_imports_and_exports\(", edge_extractor_text, "imports/exports extractor", failures)

    require(r"extract_graph_edges\(", indexer_text, "indexer edge extraction hook", failures)
    require(r"persist_graph_edges\(", indexer_text, "edge persistence helper", failures)
    require(r"get_graph_edge_table_name\(", indexer_text, "edge table helper", failures)
    require(r"fetch_edges_by_puid\(", indexer_text, "edge fetch helper", failures)

    print("Phase 2 baseline summary")
    print("------------------------")
    print("Edge contract: imports, exports, calls, inherits, implements, contains, depends_on")
    print("Resolution statuses: resolved, ambiguous, external, unresolved")
    print("Pipeline: edges extracted per file and persisted to a dedicated edge table")

    if failures:
        print()
        print("Phase 2 checks failed:")
        for failure in failures:
            print(f"- {failure}")
        return 1

    print()
    print("Phase 2 checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
