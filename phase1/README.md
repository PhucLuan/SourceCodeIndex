# Phase 1 Baseline

Phase 1 normalizes node identity and makes file nodes explicit.

## What changed

- Every file now gets a synthetic file node.
- Node identity uses a stable PUID shape:
  - `repo_name::relative_path::kind::qualified_name`
- AST chunks carry richer metadata:
  - `qualified_name`
  - `signature`
  - `docstring`
  - `modifiers`
  - `export_status`
  - `source_span`
- Parent identity is tracked with both parent name and parent node type.
- Graph node contract is centralized and canonicalized:
  - `file`
  - `function`
  - `class`
  - `method`
  - `module`
  - `concept`
  - `module` is reserved for namespace-style nodes in later phases.
- Language/doc adapters are centralized in `ast_extractors.py` so node name,
  signature, docstring, and modifier extraction can be reused across languages.
- `resolve_graph_node_kind(...)` in `graph_node_contract.py` is the shared
  place for parser-specific kind normalization.

## Contract file

- [graph_node_contract.py](../graph_node_contract.py)

## Scope

This phase is still structural. It does not add graph edges yet.
It prepares the data model for later relationship extraction.
