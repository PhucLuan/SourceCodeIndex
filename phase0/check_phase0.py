from __future__ import annotations

import json
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
FILES = {
    "app": ROOT / "app.py",
    "indexer": ROOT / "indexer_flow.py",
    "rag": ROOT / "rag.py",
    "ast": ROOT / "ast_chunker.py",
    "golden": ROOT / "phase0" / "golden_queries.json",
}


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def require(pattern: str, text: str, label: str, failures: list[str]) -> None:
    if not re.search(pattern, text, flags=re.MULTILINE):
        failures.append(f"missing {label}: /{pattern}/")


def load_golden_queries(path: Path) -> list[dict]:
    data = json.loads(read_text(path))
    if not isinstance(data, list):
        raise ValueError("golden_queries.json must contain a list")
    return data


def main() -> int:
    failures: list[str] = []

    app_text = read_text(FILES["app"])
    indexer_text = read_text(FILES["indexer"])
    rag_text = read_text(FILES["rag"])
    ast_text = read_text(FILES["ast"])
    golden = load_golden_queries(FILES["golden"])

    require(r"query_cocoindex_db", app_text, "query entrypoint", failures)
    require(r"generate_answer_stream", app_text, "answer stream", failures)
    require(r"search_per_repo", rag_text, "per-repo search", failures)
    require(r"fulltext_search_per_repo", rag_text, "per-repo full-text search", failures)
    require(r"fetch_nodes", rag_text, "node fetch helper", failures)
    require(r"SUPPORTED_AST_LANGS", indexer_text, "language support list", failures)
    require(r"parent_puid", indexer_text, "parent metadata", failures)
    require(r"references", ast_text, "AstChunk references field", failures)

    expected_ids = {f"Q{idx:02d}" for idx in range(1, 11)}
    actual_ids = {item.get("id") for item in golden}
    if actual_ids != expected_ids:
        failures.append(
            "golden query ids must be exactly Q01..Q10"
        )

    for idx, item in enumerate(golden, start=1):
        for field in ("id", "query", "intent", "expected_evidence", "expected_failure_mode"):
            if field not in item:
                failures.append(f"golden item {idx} missing field: {field}")
        if not isinstance(item.get("expected_evidence"), list) or not item["expected_evidence"]:
            failures.append(f"golden item {item.get('id', idx)} must have non-empty expected_evidence list")
        if not isinstance(item.get("expected_failure_mode"), list) or not item["expected_failure_mode"]:
            failures.append(f"golden item {item.get('id', idx)} must have non-empty expected_failure_mode list")

    print("Phase 0 baseline summary")
    print("------------------------")
    print("Search modes: semantic, full-text, hybrid, per-repo, reranker, query expansion, parent skeleton enrichment")
    print("Languages: Python, C#, JavaScript, TypeScript, TSX, HTML, CSS, SCSS, LESS")
    print(f"Golden queries: {len(golden)}")
    print()
    for item in golden:
        print(f"{item['id']}: {item['intent']} -> {item['query']}")

    if failures:
        print()
        print("Phase 0 checks failed:")
        for failure in failures:
            print(f"- {failure}")
        return 1

    print()
    print("Phase 0 checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

