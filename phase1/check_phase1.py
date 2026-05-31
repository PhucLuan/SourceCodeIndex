from __future__ import annotations

from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parent.parent
FILES = {
    "ast": ROOT / "ast_chunker.py",
    "extractors": ROOT / "ast_extractors.py",
    "indexer": ROOT / "indexer_flow.py",
    "rag": ROOT / "rag.py",
    "app": ROOT / "app.py",
    "contract": ROOT / "graph_node_contract.py",
}


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def require(pattern: str, text: str, label: str, failures: list[str]) -> None:
    if not re.search(pattern, text, flags=re.MULTILINE):
        failures.append(f"missing {label}: /{pattern}/")


def main() -> int:
    failures: list[str] = []

    ast_text = read_text(FILES["ast"])
    extractors_text = read_text(FILES["extractors"])
    indexer_text = read_text(FILES["indexer"])
    rag_text = read_text(FILES["rag"])
    app_text = read_text(FILES["app"])
    contract_text = read_text(FILES["contract"])

    require(r"class AstChunk:", ast_text, "AstChunk dataclass", failures)
    for field in [
        "qualified_name",
        "signature",
        "docstring",
        "modifiers",
        "export_status",
        "source_span",
        "parent_node_type",
        "parent_qualified_name",
    ]:
        require(rf"\b{field}\b", ast_text, f"AstChunk field {field}", failures)

    require(r"def normalize_puid\(", indexer_text, "normalize_puid helper", failures)
    require(r"def _build_file_chunk\(", indexer_text, "file node helper", failures)
    require(r"canonicalize_node_kind", indexer_text, "node kind canonicalization", failures)
    require(r"qualified_name", indexer_text, "qualified_name wiring", failures)
    require(r"signature", indexer_text, "signature wiring", failures)
    require(r"docstring", indexer_text, "docstring wiring", failures)
    require(r"modifiers", indexer_text, "modifiers wiring", failures)
    require(r"export_status", indexer_text, "export_status wiring", failures)
    require(r"source_span", indexer_text, "source_span wiring", failures)
    require(r"parent_node_type", indexer_text, "parent node type wiring", failures)

    require(r"class GraphNodeContract:", contract_text, "graph node contract dataclass", failures)
    require(r"GRAPH_NODE_KINDS", contract_text, "graph node kinds contract", failures)
    require(r"def canonicalize_node_kind\(", contract_text, "graph kind canonicalizer", failures)
    require(r"def resolve_graph_node_kind\(", contract_text, "graph kind resolver", failures)
    require(r"def extract_modifiers\(", contract_text, "modifier extractor", failures)
    for kind in ["file", "function", "class", "method", "module", "concept"]:
        require(rf"\b{kind}\b", contract_text, f"graph kind {kind}", failures)

    require(r"def extract_node_name\(", extractors_text, "shared node-name extractor", failures)
    require(r"def extract_signature\(", extractors_text, "shared signature extractor", failures)
    require(r"def extract_docstring\(", extractors_text, "shared docstring extractor", failures)
    require(r"def register_docstring_extractor\(", extractors_text, "docstring extractor registry", failures)
    require(r"def build_modifiers\(", extractors_text, "shared modifier builder", failures)

    require(r"qualified_name", rag_text, "RAG metadata passthrough", failures)
    require(r"source_span", rag_text, "RAG source span passthrough", failures)
    require(r"modifiers", rag_text, "RAG modifiers passthrough", failures)
    require(r"qualified_name", app_text, "UI qualified name display", failures)

    print("Phase 1 baseline summary")
    print("------------------------")
    print("Node identity: repo_name::relative_path::kind::qualified_name")
    print("File nodes: explicit synthetic file chunk added before content chunks")
    print("Metadata: qualified_name, signature, docstring, modifiers, export_status, source_span")
    print("Graph contract: file, function, class, method, module, concept")
    print("Reusable extractors: shared node-name, signature, docstring registry, modifier helpers")

    if failures:
        print()
        print("Phase 1 checks failed:")
        for failure in failures:
            print(f"- {failure}")
        return 1

    print()
    print("Phase 1 checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
