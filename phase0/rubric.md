# Phase 0 Evaluation Rubric

This rubric is used to judge the baseline and later regressions.

## Scoring dimensions

Each query is scored from `0` to `3` for each dimension.

### Relevance

- `3`: The top result matches the asked intent directly.
- `2`: The answer is on-topic but misses one important detail.
- `1`: The answer is weakly related and mostly noisy.
- `0`: The answer is off-topic.

### Precision

- `3`: The answer names the correct node, file, or relation.
- `2`: Mostly correct with one minor ambiguity.
- `1`: Partially correct but too broad.
- `0`: Wrong or speculative.

### Coverage

- `3`: All expected evidence is present.
- `2`: Most evidence is present.
- `1`: Only one useful clue is present.
- `0`: Evidence is missing.

### Citation quality

- `3`: Evidence is tied to file and line references.
- `2`: Evidence is cited at file level only.
- `1`: Evidence is described without clear provenance.
- `0`: No citation.

### Graph honesty

- `3`: The answer only claims relations that are supported.
- `2`: Mostly honest, with minor overreach that is easy to correct.
- `1`: Some unsupported graph language appears.
- `0`: The answer invents edges or node relationships.

## Pass criteria

- Exact-symbol or direct-graph queries must put the correct node or edge first.
- Graph questions must return graph evidence, not just similar snippets.
- If the context is insufficient, the system must say so explicitly.
- A solution passes only when the total score is consistently strong across the full golden set.

## Baseline note

Phase 0 is intentionally conservative. The current system is allowed to fail graph queries as long as it does not hallucinate them.

