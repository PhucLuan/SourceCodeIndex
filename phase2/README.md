Phase 2 adds deterministic graph edge extraction.

## What changed

- Edge contract is centralized in `graph_edge_contract.py`.
- Relationship extraction is centralized in `graph_edge_extractor.py`.
- The index pipeline now persists a separate edge table alongside node rows.
- Supported edge types:
  - `imports`
  - `exports`
  - `calls`
  - `inherits`
  - `implements`
  - `contains`
  - `depends_on`
- Resolution status is tracked explicitly:
  - `resolved`
  - `ambiguous`
  - `external`
  - `unresolved`

## Scope

This phase stores deterministic edges and unresolved placeholders.
It does not yet build graph traversal or cross-file linker logic.

