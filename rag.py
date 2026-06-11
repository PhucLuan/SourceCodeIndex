"""RAG layer for source code search."""

from __future__ import annotations

import os
import re
import sys
import time
from typing import Any, Optional

import asyncpg
import streamlit as st
from langchain_core.documents import Document
from langchain_core.prompts import PromptTemplate
from langchain_ollama import OllamaLLM

from indexer_flow import (
    DATABASE_URL,
    PG_SCHEMA,
    TABLE_NAME,
    fetch_edges_by_puid,
    fetch_nodes,
    fulltext_search as _fulltext_search,
    fulltext_search_per_repo,
    get_all_repo_names,
    get_graph_edge_table_name,
    rrf_merge,
    search as _search,
    search_per_repo,
)


SCORE_THRESHOLD = 0.3
_RERANKER_MODEL = None

GRAPH_INTENT_EDGE_MAP: dict[str, set[str]] = {
    "semantic": {"contains", "imports", "calls"},
    "symbol lookup": {"contains", "exports"},
    "dependency": {"imports", "exports", "depends_on", "contains"},
    "call flow": {"calls", "contains"},
    "impact analysis": {"calls", "imports", "inherits", "implements", "contains", "depends_on"},
    "architecture tour": {"contains", "imports", "calls", "inherits", "implements", "depends_on"},
    "domain/business flow": {"calls", "imports", "contains", "depends_on"},
}


def get_graph_edge_types_for_intent(intent: str) -> set[str]:
    return set(GRAPH_INTENT_EDGE_MAP.get(intent, GRAPH_INTENT_EDGE_MAP["semantic"]))


def get_reranker_model():
    global _RERANKER_MODEL
    if _RERANKER_MODEL is None:
        from sentence_transformers import CrossEncoder

        sys.stderr.write("\n[RERANKER] Loading CrossEncoder model...\n")
        sys.stderr.flush()
        _RERANKER_MODEL = CrossEncoder("cross-encoder/ms-marco-MiniLM-L-6-v2")
        sys.stderr.write("[RERANKER] Model loaded successfully.\n")
        sys.stderr.flush()
    return _RERANKER_MODEL


def rerank(query: str, candidates: list[dict], top_k: int) -> list[dict]:
    if not candidates:
        return []

    model = get_reranker_model()
    pairs = [(query, c["text"]) for c in candidates]
    start_time = time.perf_counter()
    scores = model.predict(pairs)
    duration = time.perf_counter() - start_time

    for c, score in zip(candidates, scores):
        c["_rerank_score"] = float(score)
        c["score_type"] = "rerank"

    reranked = sorted(candidates, key=lambda x: x["_rerank_score"], reverse=True)
    sys.stderr.write(f"\n[RERANKER] Reranked {len(candidates)} candidates in {duration:.4f}s\n")
    sys.stderr.flush()
    return reranked[:top_k]


def expand_query(query_text: str, llm) -> list[str]:
    if not llm:
        return [query_text]

    prompt = f"""You are a source code search expert.
Create 2-3 short query variants in English or Vietnamese.
Focus on technical keywords such as class, method, API, logic.
Return only one variant per line.

Question: {query_text}"""

    try:
        response = llm.invoke(prompt)
        variants = [v.strip("- ").strip() for v in response.split("\n") if v.strip()]
        return [query_text] + variants[:3]
    except Exception as e:
        sys.stderr.write(f"[WARNING] Query expansion failed: {e}\n")
        return [query_text]


def detect_query_intent(query_text: str) -> str:
    text = (query_text or "").strip().lower()
    if not text:
        return "semantic"

    rules = [
        ("impact analysis", [r"\b(anh huong|impact|affected|neu .* sua|doi .* thi ai bi)\b", r"\breverse dependency\b"]),
        ("call flow", [r"\b(goi\s+.*\bham nao\b|goi gi|goi ham nao|call flow|ai goi|ham nay goi)\b"]),
        ("dependency", [r"\b(import|phu thuoc|depends on|module nao import|file nao import)\b"]),
        ("symbol lookup", [r"\b(o dau|tim .*|where is|symbol lookup|ten gan giong|fuzzy)\b"]),
        ("architecture tour", [r"\b(tour|huong dan doc|bat dau tu dau|architecture|structure)\b"]),
        ("domain/business flow", [r"\b(ui toi api|ui toi db|luong xu ly|business flow|request flow)\b", r"\b(login|auth)\b"]),
    ]

    for intent, patterns in rules:
        if any(re.search(pattern, text) for pattern in patterns):
            return intent
    return "semantic"


def lookup_symbol(name: str, repo_name: Optional[str] = None, fuzzy: bool = False) -> list[dict]:
    params: list[object] = [name]
    clauses = ["is_skeleton = FALSE"]

    if fuzzy:
        params.append(f"%{name.lower()}%")
        clauses.append(
            "(LOWER(COALESCE(node_name, '')) LIKE $2 OR LOWER(COALESCE(qualified_name, '')) LIKE $2 OR LOWER(COALESCE(filename, '')) LIKE $2)"
        )
        if repo_name:
            params.append(repo_name)
            clauses.append(f"repo_name = ${len(params)}")
        query = f"""
            SELECT puid, filename, repo_name, node_type, node_name, qualified_name,
                   signature, docstring, modifiers, export_status, lang,
                   CASE
                       WHEN LOWER(COALESCE(node_name, '')) = LOWER($1) THEN 1.0
                       WHEN LOWER(COALESCE(qualified_name, '')) = LOWER($1) THEN 0.98
                       WHEN LOWER(COALESCE(node_name, '')) LIKE LOWER($2) THEN 0.85
                       WHEN LOWER(COALESCE(qualified_name, '')) LIKE LOWER($2) THEN 0.8
                       WHEN LOWER(COALESCE(filename, '')) LIKE LOWER($2) THEN 0.75
                       ELSE 0.5
                   END AS score
            FROM "{PG_SCHEMA}"."{TABLE_NAME}"
            WHERE {" AND ".join(clauses)}
            ORDER BY score DESC, node_name ASC, qualified_name ASC
            LIMIT 10
        """
    else:
        if repo_name:
            params.append(repo_name)
            clauses.append(f"repo_name = ${len(params)}")
        clauses.append("(node_name = $1 OR qualified_name = $1 OR filename = $1)")
        query = f"""
            SELECT puid, filename, repo_name, node_type, node_name, qualified_name,
                   signature, docstring, modifiers, export_status, lang,
                   1.0 AS score
            FROM "{PG_SCHEMA}"."{TABLE_NAME}"
            WHERE {" AND ".join(clauses)}
            ORDER BY node_type ASC, node_name ASC, qualified_name ASC
            LIMIT 10
        """

    async def _run() -> list[dict]:
        async with await asyncpg.create_pool(DATABASE_URL) as pool:
            rows = await pool.fetch(query, *params)
            return [dict(r) for r in rows]

    try:
        import asyncio

        loop = asyncio.get_event_loop()
        return loop.run_until_complete(_run())
    except Exception:
        return []


def _enforce_soft_quota(merged: list[dict], top_k: int, max_per_repo: int) -> list[dict]:
    repo_counts: dict[str, int] = {}
    primary: list[dict] = []
    overflow: list[dict] = []

    for item in merged:
        repo = item.get("repo_name", "__unknown__")
        cnt = repo_counts.get(repo, 0)
        if cnt < max_per_repo:
            primary.append(item)
            repo_counts[repo] = cnt + 1
        else:
            overflow.append(item)
        if len(primary) >= top_k:
            break

    need = top_k - len(primary)
    return primary + overflow[:need]


def _graph_edges_to_prompt_block(edges: list[dict[str, Any]]) -> str:
    if not edges:
        return "No graph edges were retrieved."
    lines = []
    for edge in edges[:40]:
        src = edge.get("source_node") or edge.get("source_symbol") or edge.get("source_puid") or ""
        tgt = edge.get("target_node") or edge.get("target_symbol") or edge.get("target_puid") or ""
        lines.append(
            f"- {src} -> {tgt} "
            f"[type={edge.get('edge_type','')}, status={edge.get('resolution_status','')}, confidence={float(edge.get('confidence',0.0)):.2f}]"
        )
    return "\n".join(lines)


def _format_mermaid_from_edges(edges: list[dict[str, Any]]) -> str:
    if not edges:
        return "graph LR\n  A[No graph evidence]"

    def safe_label(value: str) -> str:
        text = re.sub(r"[^A-Za-z0-9_]+", "_", value or "node").strip("_")
        return text or "node"

    lines = ["graph LR"]
    for edge in edges[:20]:
        src = edge.get("source_node") or edge.get("source_symbol") or edge.get("source_puid") or "source"
        tgt = edge.get("target_node") or edge.get("target_symbol") or edge.get("target_puid") or "target"
        src_id = safe_label(src)
        tgt_id = safe_label(tgt)
        arrow = "-->" if edge.get("edge_type") != "contains" else "-.->"
        lines.append(f'  {src_id}["{src}"] {arrow} {tgt_id}["{tgt}"]')
    return "\n".join(lines)


def build_answer_payload(query_text: str, docs: list[Document]) -> dict[str, str]:
    """Build prompt payload for tests and prompt assembly."""
    context_parts: list[str] = []
    graph_edges: list[dict[str, Any]] = list(getattr(st.session_state, "graph_seed_edges", []))

    for d in docs:
        meta = d.metadata
        filename = meta.get("filename", "unknown")
        start = meta.get("start_line", "?")
        end = meta.get("end_line", "?")
        score = meta.get("score", 0)
        is_test = meta.get("is_test", False)
        node_type = meta.get("node_type", "")
        node_name = meta.get("node_name", "")
        qualified_name = meta.get("qualified_name", "")
        signature = meta.get("signature", "")
        source_span = meta.get("source_span", "")
        docstring = meta.get("docstring", "")
        modifiers = meta.get("modifiers", "")
        puid = meta.get("puid", "")
        is_skeleton = meta.get("is_skeleton", False)

        tag = " [TEST FILE]" if is_test else ""
        if is_skeleton:
            tag += " [SKELETON]"

        node_info = f" [{node_type.upper()}: {node_name}]" if node_type and node_name else ""
        qname_info = f"\nQualified: {qualified_name}" if qualified_name else ""
        sig_info = f"\nSignature: {signature}" if signature else ""
        span_info = f"\nSpan: {source_span}" if source_span else ""
        doc_info = f"\nDocstring: {docstring}" if docstring else ""
        mod_info = f"\nModifiers: {modifiers}" if modifiers else ""
        puid_info = f"\nPUID: {puid}" if puid else ""

        context_parts.append(
            f"--- {filename}:L{start}-L{end}{node_info}{tag}{qname_info}{sig_info}{span_info}{doc_info}{mod_info}{puid_info} (relevance: {score:.3f}) ---\n"
            f"{d.page_content}"
        )

        if puid:
            try:
                graph_edges.extend(fetch_edges_by_puid([puid], direction="both"))
            except Exception:
                pass

    return {
        "context": "\n\n".join(context_parts),
        "graph_evidence": f"Intent: {detect_query_intent(query_text)}\n" + _graph_edges_to_prompt_block(graph_edges),
        "mermaid_graph": _format_mermaid_from_edges(graph_edges),
        "intent": detect_query_intent(query_text),
    }


def query_cocoindex_db(
    query_text: str,
    top_k: int = 8,
    source_filters: list[str] = None,
    llm=None,
    similarity_threshold: float = 0.3,
    use_query_expansion: bool = True,
    use_hybrid: bool = True,
    use_reranker: bool = False,
) -> list[Document]:
    try:
        intent = detect_query_intent(query_text)
        graph_edge_types = GRAPH_INTENT_EDGE_MAP.get(intent, GRAPH_INTENT_EDGE_MAP["semantic"])
        queries = expand_query(query_text, llm) if (llm and use_query_expansion) else [query_text]

        if source_filters:
            repo_names = list(source_filters)
        else:
            try:
                repo_names = get_all_repo_names()
            except Exception as ex:
                sys.stderr.write(f"[WARN] get_all_repo_names failed: {ex}; falling back to global search.\n")
                repo_names = []

        use_per_repo = len(repo_names) > 1
        search_limit = top_k * 2
        all_results: list[dict] = []
        seen_puids: set[str] = set()
        rejected_count = 0

        sys.stderr.write(
            f"\n[RETRIEVAL_START] intent={intent} | {len(queries)} queries x {len(repo_names) if use_per_repo else 1} scope(s)\n"
        )

        if intent == "symbol lookup":
            symbol_hits: list[dict] = []
            for repo in (repo_names or [None]):
                hits = lookup_symbol(query_text, repo_name=repo, fuzzy=False)
                if not hits:
                    hits = lookup_symbol(query_text, repo_name=repo, fuzzy=True)
                symbol_hits.extend(hits)
            symbol_hits.sort(key=lambda x: x.get("score", 0.0), reverse=True)
            all_results.extend(symbol_hits[:search_limit])
        elif use_per_repo:
            per_repo_hits: dict[str, list[dict]] = {r: [] for r in repo_names}
            seen_per_repo: dict[str, set[str]] = {r: set() for r in repo_names}

            for idx, q in enumerate(queries):
                for repo in repo_names:
                    vec_hits = search_per_repo(q, top_k=search_limit, repo_name=repo)
                    merged = rrf_merge(vec_hits, fulltext_search_per_repo(q, top_k=search_limit, repo_name=repo), k=60) if use_hybrid else vec_hits
                    for item in merged:
                        puid = item.get("puid", "")
                        if puid and puid not in seen_per_repo[repo]:
                            per_repo_hits[repo].append(item)
                            seen_per_repo[repo].add(puid)
                sys.stderr.write(f"  - Query {idx}: '{q[:50]}' done across {len(repo_names)} repos.\n")

            for repo in repo_names:
                per_repo_hits[repo].sort(key=lambda x: x.get("_rrf_score", x["score"]), reverse=True)
                per_repo_hits[repo] = per_repo_hits[repo][:search_limit]

            all_repo_lists = list(per_repo_hits.values())
            global_merged = all_repo_lists[0] if len(all_repo_lists) == 1 else []
            for other in all_repo_lists[1:]:
                global_merged = rrf_merge(global_merged, other, k=60) if global_merged else other
            global_merged.sort(key=lambda x: x.get("_rrf_score", x["score"]), reverse=True)

            import math
            max_per_repo = math.ceil(top_k / max(len(repo_names), 1)) + 1
            candidates = _enforce_soft_quota(global_merged, top_k * 2, max_per_repo)

            for item in candidates:
                score = item.get("_rrf_score", item["score"])
                if not use_hybrid and score < similarity_threshold:
                    rejected_count += 1
                    continue
                puid = item.get("puid", "")
                if puid not in seen_puids:
                    all_results.append(item)
                    seen_puids.add(puid)
        else:
            for idx, q in enumerate(queries):
                vec_hits = _search(q, top_k=search_limit, source_filters=source_filters)
                results = rrf_merge(vec_hits, _fulltext_search(q, top_k=search_limit, source_filters=source_filters), k=60) if use_hybrid else vec_hits
                count_valid = 0
                for item in results:
                    score = item.get("_rrf_score", item["score"])
                    if not use_hybrid and score < similarity_threshold:
                        rejected_count += 1
                        continue
                    puid = item.get("puid", "")
                    if puid not in seen_puids:
                        all_results.append(item)
                        seen_puids.add(puid)
                        count_valid += 1
                sys.stderr.write(f"  - Query {idx}: '{q[:50]}' -> {count_valid} new results.\n")

        # Graph-aware expansion from seeds.
        seed_puids = [r.get("puid") for r in all_results[: min(len(all_results), top_k)] if r.get("puid")]
        if seed_puids and intent != "symbol lookup":
            try:
                seed_edges = fetch_edges_by_puid(seed_puids, direction="both")
            except Exception as ex:
                sys.stderr.write(f"[WARN] fetch_edges_by_puid failed: {ex}\n")
                seed_edges = []

            filtered_edges = [e for e in seed_edges if (e.get("edge_type") or "") in graph_edge_types]
            candidate_puids: list[str] = []
            for edge in filtered_edges:
                for key in ("source_puid", "target_puid"):
                    puid = edge.get(key) or ""
                    if puid and puid not in seen_puids and puid not in candidate_puids:
                        candidate_puids.append(puid)

            if candidate_puids:
                try:
                    graph_nodes = fetch_nodes(candidate_puids)
                except Exception as ex:
                    sys.stderr.write(f"[WARN] fetch_nodes(graph expansion) failed: {ex}\n")
                    graph_nodes = []
                for node in graph_nodes:
                    node["score"] = max(node.get("score", 0.0), 0.88)
                    node["score_type"] = "graph_expansion"
                    node["query_intent"] = intent
                    all_results.append(node)
                    seen_puids.add(node.get("puid", ""))

            try:
                st.session_state.graph_seed_edges = filtered_edges[:50]
            except Exception:
                pass

        try:
            st.session_state.rejected_count = rejected_count
            st.session_state.last_query_intent = intent
        except Exception:
            pass

        all_results.sort(key=lambda x: x.get("_rrf_score", x["score"]), reverse=True)
        if use_reranker:
            final_results = rerank(query_text, all_results, top_k)
        else:
            for item in all_results:
                item["score_type"] = "rrf" if use_hybrid else "cosine"
            final_results = all_results[:top_k]

        enriched_results = list(final_results)
        parent_puids = {r["parent_puid"] for r in final_results if r.get("parent_puid")}
        puids_to_fetch = [p for p in parent_puids if p not in seen_puids]
        if puids_to_fetch:
            skeletons = fetch_nodes(puids_to_fetch, is_skeleton=True)
            for skel in skeletons:
                skel["score"] = 0.99
                skel["score_type"] = "skeleton"
                skel["query_intent"] = intent
                enriched_results.append(skel)
                seen_puids.add(skel["puid"])

        return [
            Document(
                page_content=r["text"],
                metadata={
                    "filename": r["filename"],
                    "lang": r["lang"],
                    "score": r.get("_rerank_score", r.get("_rrf_score", r["score"])),
                    "score_type": r.get("score_type", "cosine_or_rrf"),
                    "start_line": r["start_line"],
                    "end_line": r["end_line"],
                    "is_test": r["is_test"],
                    "node_type": r.get("node_type", ""),
                    "node_name": r.get("node_name", ""),
                    "qualified_name": r.get("qualified_name", ""),
                    "signature": r.get("signature", ""),
                    "docstring": r.get("docstring", ""),
                    "modifiers": r.get("modifiers", ""),
                    "export_status": r.get("export_status", ""),
                    "source_span": r.get("source_span", ""),
                    "puid": r.get("puid", ""),
                    "parent_puid": r.get("parent_puid", ""),
                    "is_skeleton": r.get("is_skeleton", False),
                    "repo_name": r.get("repo_name", ""),
                    "query_intent": intent,
                },
            )
            for r in enriched_results
        ]
    except Exception as e:
        if "does not exist" in str(e).lower():
            return []
        st.error(f"Error when querying CocoIndex: {e}")
        import logging

        logging.getLogger(__name__).error(f"Search Error: {e}", exc_info=True)
        return []


def get_llm(llm_choice, model_name="gemma3:4b", api_key=None, ollama_host=None):
    if llm_choice == "Ollama":
        url = ollama_host or os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
        return OllamaLLM(model=model_name, base_url=url)
    if llm_choice in ("OpenAI", "Gemini"):
        raise NotImplementedError(
            f"{llm_choice} is not supported yet. Install the corresponding LangChain provider first."
        )
    raise ValueError(f"Unknown LLM choice: {llm_choice}")


def generate_answer_stream(query_text: str, docs: list[Document], llm):
    prompt_template = """\
You are a senior software engineer helping analyze a source code base.

<context>
{context}
</context>

<graph_evidence>
{graph_evidence}
</graph_evidence>

<mermaid>
{mermaid_graph}
</mermaid>

Answering rules:
1. Use the code context and graph evidence together.
2. When citing code, include file name and line numbers if available.
3. If the query is graph-oriented, prefer edges, adjacency list, or Mermaid.
4. If the context is insufficient, say so clearly.
5. Answer in Vietnamese if the question is in Vietnamese.

Question: {question}
"""

    prompt = PromptTemplate.from_template(prompt_template)
    payload = build_answer_payload(query_text, docs)
    chain = prompt | llm

    for chunk in chain.stream(
        {
            "context": payload["context"],
            "question": query_text,
            "graph_evidence": payload["graph_evidence"],
            "mermaid_graph": payload["mermaid_graph"],
        }
    ):
        if hasattr(chunk, "content"):
            yield chunk.content
        else:
            yield str(chunk)


async def get_neighbors(puid: str) -> list[dict]:
    edge_table = get_graph_edge_table_name(TABLE_NAME)
    query = f"""
        SELECT e.edge_type, e.resolution_status, e.confidence, e.source_puid, e.target_puid,
               e.source_symbol, e.target_symbol, e.source_line, e.target_line,
               n.filename, n.node_type, n.node_name, n.text
        FROM "{PG_SCHEMA}"."{edge_table}" e
        LEFT JOIN "{PG_SCHEMA}"."{TABLE_NAME}" n
          ON (e.target_puid = n.puid AND e.source_puid = $1)
          OR (e.source_puid = n.puid AND e.target_puid = $1)
        WHERE e.source_puid = $1 OR e.target_puid = $1
    """
    async with await asyncpg.create_pool(DATABASE_URL) as pool:
        rows = await pool.fetch(query, puid)
        return [dict(r) for r in rows]


async def get_incoming_edges(puid: str) -> list[dict]:
    edge_table = get_graph_edge_table_name(TABLE_NAME)
    query = f"""
        SELECT e.edge_type, e.resolution_status, e.confidence, e.source_puid, e.target_puid,
               e.source_symbol, e.target_symbol, e.source_line, e.target_line,
               n.filename, n.node_type, n.node_name, n.text
        FROM "{PG_SCHEMA}"."{edge_table}" e
        LEFT JOIN "{PG_SCHEMA}"."{TABLE_NAME}" n ON e.source_puid = n.puid
        WHERE e.target_puid = $1
    """
    async with await asyncpg.create_pool(DATABASE_URL) as pool:
        rows = await pool.fetch(query, puid)
        return [dict(r) for r in rows]


async def get_outgoing_edges(puid: str) -> list[dict]:
    edge_table = get_graph_edge_table_name(TABLE_NAME)
    query = f"""
        SELECT e.edge_type, e.resolution_status, e.confidence, e.source_puid, e.target_puid,
               e.source_symbol, e.target_symbol, e.source_line, e.target_line,
               n.filename, n.node_type, n.node_name, n.text
        FROM "{PG_SCHEMA}"."{edge_table}" e
        LEFT JOIN "{PG_SCHEMA}"."{TABLE_NAME}" n ON e.target_puid = n.puid
        WHERE e.source_puid = $1
    """
    async with await asyncpg.create_pool(DATABASE_URL) as pool:
        rows = await pool.fetch(query, puid)
        return [dict(r) for r in rows]


async def get_shortest_path(start_puid: str, end_puid: str, max_depth: int = 5) -> list[dict]:
    edge_table = get_graph_edge_table_name(TABLE_NAME)
    query = f"""
        SELECT source_puid, target_puid, edge_type, resolution_status
        FROM "{PG_SCHEMA}"."{edge_table}"
        WHERE resolution_status = 'resolved'
    """
    async with await asyncpg.create_pool(DATABASE_URL) as pool:
        rows = await pool.fetch(query)
        edges = [dict(r) for r in rows]

    adj: dict[str, list[dict]] = {}
    for edge in edges:
        adj.setdefault(edge["source_puid"], []).append(edge)

    from collections import deque

    queue = deque([[start_puid]])
    visited = {start_puid}

    while queue:
        path = queue.popleft()
        node = path[-1]
        if node == end_puid:
            result_path = []
            for i in range(len(path) - 1):
                u = path[i]
                v = path[i + 1]
                matching_edges = [e for e in adj.get(u, []) if e["target_puid"] == v]
                edge_type = matching_edges[0]["edge_type"] if matching_edges else "depends_on"
                result_path.append({"source": u, "target": v, "edge_type": edge_type})
            return result_path

        if len(path) > max_depth:
            continue

        for edge in adj.get(node, []):
            nxt = edge["target_puid"]
            if nxt not in visited:
                visited.add(nxt)
                queue.append(path + [nxt])

    return []
