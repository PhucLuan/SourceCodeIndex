# Phase 0 Baseline

This directory captures the current baseline for the graph search roadmap.
It is the reference point for later phases, not the target end-state.

## Codebase inventory

### Language support

- Python
- C#
- JavaScript
- TypeScript
- TSX
- HTML
- CSS
- SCSS
- LESS

### Current node metadata

The index currently stores these fields for each chunk/node:

- `filename`
- `lang`
- `text`
- `embedding`
- `start_line`
- `end_line`
- `is_test`
- `node_type`
- `node_name`
- `puid`
- `parent_puid`
- `is_skeleton`
- `repo_name`

### Current search modes

- Vector semantic search
- Full-text search over `tsvector`
- Hybrid search via RRF fusion
- Per-repo vector search
- Per-repo full-text search
- Optional reranker
- Query expansion before retrieval
- Context enrichment via parent skeleton lookup

### Central files

- [app.py](../app.py)
- [indexer_flow.py](../indexer_flow.py)
- [rag.py](../rag.py)
- [ast_chunker.py](../ast_chunker.py)

### Current limitations

- No edge table for `imports`, `calls`, `inherits`, `contains`, `exports`, or `depends_on`
- No symbol resolver that links `target_symbol` to a concrete node
- No explicit file node contract
- `AstChunk.references` exists but is not used to generate graph relationships
- Graph traversal is limited to one-hop parent skeleton enrichment
- No intent classifier that separates semantic search, graph traversal, and impact analysis

## Baseline expectation

The current system can answer:

- code snippet search
- hybrid semantic plus lexical retrieval
- reranked retrieval
- repository-scoped retrieval

The current system cannot yet answer true graph questions such as:

- direct imports
- call graph
- reverse dependency impact
- layer traversal

Those are the target of later phases.

