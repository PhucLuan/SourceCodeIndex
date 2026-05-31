"""
RAG layer — kết nối CocoIndex search với LangChain LLM.

Cải tiến:
- query_cocoindex_db: Sử dụng pure semantic search để phân biệt logic/test tự nhiên
- generate_answer_stream: Có prompt chuyên về code search + hướng dẫn LLM
"""

import os
import time
import sys
from langchain_core.documents import Document
from langchain_ollama import OllamaLLM
from langchain_core.prompts import PromptTemplate

import streamlit as st

from indexer_flow import (
    search as _search,
    fetch_nodes,
    fulltext_search as _fulltext_search,
    rrf_merge,
    get_all_repo_names,
    search_per_repo,
    fulltext_search_per_repo,
)


SCORE_THRESHOLD = 0.3

_RERANKER_MODEL = None

def get_reranker_model():
    """Tải CrossEncoder model theo mô hình Singleton."""
    global _RERANKER_MODEL
    if _RERANKER_MODEL is None:
        import sys
        from sentence_transformers import CrossEncoder
        sys.stderr.write("\n[RERANKER] Loading CrossEncoder model: cross-encoder/ms-marco-MiniLM-L-6-v2...\n")
        sys.stderr.flush()
        _RERANKER_MODEL = CrossEncoder("cross-encoder/ms-marco-MiniLM-L-6-v2")
        sys.stderr.write("[RERANKER] Model loaded successfully.\n")
        sys.stderr.flush()
    return _RERANKER_MODEL

def rerank(query: str, candidates: list[dict], top_k: int) -> list[dict]:
    """Chấm điểm chéo các ứng cử viên bằng Cross-Encoder."""
    if not candidates:
        return []
    
    model = get_reranker_model()
    # Tạo các cặp (query, document_text)
    pairs = [(query, c["text"]) for c in candidates]
    
    start_time = time.perf_counter()
    scores = model.predict(pairs)
    duration = time.perf_counter() - start_time
    
    # Gán score mới và đánh dấu score_type
    for c, score in zip(candidates, scores):
        c["_rerank_score"] = float(score)
        c["score_type"] = "rerank"
        
    # Sắp xếp lại theo điểm reranker giảm dần
    reranked = sorted(candidates, key=lambda x: x["_rerank_score"], reverse=True)
    
    sys.stderr.write(f"\n[RERANKER] Reranked {len(candidates)} candidates in {duration:.4f}s\n")
    for i, r in enumerate(reranked[:10]):
        sys.stderr.write(f"  {i+1}. [RERANKED] {r['puid']} (rerank_score: {r['_rerank_score']:.4f}, old_score: {r.get('_rrf_score', r['score']):.4f})\n")
    sys.stderr.write("=============================================\n")
    sys.stderr.flush()
    
    return reranked[:top_k]


def expand_query(query_text: str, llm) -> list[str]:
    """Sử dụng LLM để tạo ra các biến thể kỹ thuật của câu hỏi giúp tăng recall."""
    if not llm:
        return [query_text]
    
    prompt = f"""Bạn là một chuyên gia tìm kiếm mã nguồn. 
Hãy tạo ra 2-3 biến thể ngắn gọn của câu hỏi sau bằng tiếng Anh và tiếng Việt, tập trung vào các từ khóa kỹ thuật (class, method, API, logic).
Chỉ trả về danh sách các biến thể, mỗi biến thể một dòng, không giải thích.

Câu hỏi: {query_text}"""
    
    try:
        response = llm.invoke(prompt)
        variants = [v.strip("- ").strip() for v in response.split("\n") if v.strip()]
        queries = [query_text] + variants[:3]
        
        # === [DEBUG_LOG] ===
        sys.stderr.write(f"\n[QUERY_EXPANSION] Gốc: '{query_text}'\n")
        for i, q in enumerate(queries[1:]):
            sys.stderr.write(f"  - Biến thể {i+1}: '{q}'\n")
        sys.stderr.flush()
        
        return queries
    except Exception as e:
        sys.stderr.write(f"[WARNING] Query expansion failed: {e}\n")
        return [query_text]

# ── Soft‑quota helper ─────────────────────────────────────────────────────────

def _enforce_soft_quota(
    merged: list[dict],
    top_k: int,
    max_per_repo: int,
) -> list[dict]:
    """
    Apply a soft quota so that no single repo can monopolise the top‑k slots.

    Strategy:
      - Walk the RRF‑sorted list.
      - Keep a per‑repo counter.
      - Once a repo fills its *hard* slot (max_per_repo) we put it in a
        "overflow" bucket.  After the main pass, append overflow items until
        top_k is satisfied.
    This guarantees at least some diversity while still respecting global rank.
    """
    repo_counts: dict[str, int] = {}
    primary: list[dict] = []
    overflow: list[dict] = []

    for item in merged:
        repo = item.get("repo_name", "__unknown__")
        cnt  = repo_counts.get(repo, 0)
        if cnt < max_per_repo:
            primary.append(item)
            repo_counts[repo] = cnt + 1
        else:
            overflow.append(item)

        if len(primary) >= top_k:
            break

    # Fill remaining slots from overflow (already in global RRF order)
    need = top_k - len(primary)
    return primary + overflow[:need]


# ── Main retrieval function (Task 3.5.1 – per‑repo + soft quota) ──────────────

def query_cocoindex_db(
    query_text: str,
    top_k: int = 8,
    source_filters: list[str] = None,
    llm = None,
    similarity_threshold: float = 0.3,
    use_query_expansion: bool = True,
    use_hybrid: bool = True,
    use_reranker: bool = False,
) -> list[Document]:
    """
    Multi‑repo aware semantic search (Task 3.5.1).

    Pipeline:
      1. Query Expansion  – LLM generates 2‑3 query variants (optional).
      2. Repo Discovery   – list all repo_name values currently indexed.
      3. Per‑repo fetch   – for each (query, repo) pair run vector search
                           and optionally BM25; merge via RRF *within* repo.
      4. Global RRF       – merge per‑repo winners into a single ranked list.
      5. Soft Quota       – cap slots per repo to avoid single‑repo dominance.
      6. Score Threshold  – reject low‑confidence cosine hits (non‑hybrid).
      7. De‑duplication   – drop items already seen by PUID.
      8. Cross‑Encoder    – optional reranker pass.
      9. Context Enrich   – pull parent skeleton nodes for richer context.
    """
    try:
        # ── 1. Query Expansion ────────────────────────────────────────────────
        queries = expand_query(query_text, llm) if (llm and use_query_expansion) else [query_text]

        # ── 2. Repo Discovery ────────────────────────────────────────────────
        # If the caller supplied source_filters we search only those repos;
        # otherwise we discover all repos dynamically from the DB.
        if source_filters:
            repo_names = list(source_filters)
        else:
            try:
                repo_names = get_all_repo_names()
            except Exception as ex:
                sys.stderr.write(f"[WARN] get_all_repo_names failed: {ex}; falling back to global search.\n")
                repo_names = []

        # Decide whether to use per‑repo path or fall back to global search
        use_per_repo = len(repo_names) > 1

        search_limit = top_k * 2   # over‑fetch per repo / globally
        all_results: list[dict] = []
        seen_puids: set[str]    = set()
        rejected_count          = 0

        sys.stderr.write(
            f"\n[RETRIEVAL_START] {len(queries)} queries × "
            f"{len(repo_names) if use_per_repo else '1 (global)'} repo(s)\n"
        )

        if use_per_repo:
            # ── 3. Per‑repo fetch & intra‑repo RRF ───────────────────────────
            # Collect per‑repo merged lists for all queries.
            # key: repo_name  value: list of RRF‑merged hits
            per_repo_hits: dict[str, list[dict]] = {r: [] for r in repo_names}
            seen_per_repo: dict[str, set[str]]   = {r: set() for r in repo_names}

            for idx, q in enumerate(queries):
                for repo in repo_names:
                    vec_hits  = search_per_repo(q, top_k=search_limit, repo_name=repo)

                    if use_hybrid:
                        bm25_hits = fulltext_search_per_repo(q, top_k=search_limit, repo_name=repo)
                        merged    = rrf_merge(vec_hits, bm25_hits, k=60)
                    else:
                        merged = vec_hits

                    for item in merged:
                        puid = item.get("puid", "")
                        if puid and puid not in seen_per_repo[repo]:
                            per_repo_hits[repo].append(item)
                            seen_per_repo[repo].add(puid)

                sys.stderr.write(
                    f"  - Query {idx}: '{q[:50]}' done across {len(repo_names)} repos.\n"
                )

            # Sort each repo's list by its best score, keep top search_limit
            for repo in repo_names:
                per_repo_hits[repo].sort(
                    key=lambda x: x.get("_rrf_score", x["score"]), reverse=True
                )
                per_repo_hits[repo] = per_repo_hits[repo][:search_limit]

            # ── 4. Global RRF across repos ────────────────────────────────────
            # Build two synthetic ranked lists: all vector and all bm25 hits
            # already merged per‑repo – we RRF the per‑repo top lists together.
            # For simplicity we treat per‑repo merged lists as a single pool
            # and run one more RRF pass (lists ordered by per‑repo RRF score).
            all_repo_lists = list(per_repo_hits.values())

            if len(all_repo_lists) == 1:
                global_merged = all_repo_lists[0]
            elif len(all_repo_lists) == 0:
                global_merged = []
            else:
                # iteratively RRF‑merge multiple per‑repo lists
                global_merged = all_repo_lists[0]
                for other in all_repo_lists[1:]:
                    global_merged = rrf_merge(global_merged, other, k=60)

            global_merged.sort(
                key=lambda x: x.get("_rrf_score", x["score"]), reverse=True
            )

            # ── 5. Soft Quota ─────────────────────────────────────────────────
            # Allow at most ceil(top_k / n_repos) + 1 slots per repo so that
            # every repo gets a fair chance but top results still float up.
            import math
            n_repos    = max(len(repo_names), 1)
            max_per_repo = math.ceil(top_k / n_repos) + 1

            candidates = _enforce_soft_quota(global_merged, top_k * 2, max_per_repo)

            # ── 6. Score threshold & de‑dup ───────────────────────────────────
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
            # ── Fallback: single‑repo or no repos – use original global search ─
            for idx, q in enumerate(queries):
                vec_hits = _search(q, top_k=search_limit, source_filters=source_filters)

                if use_hybrid:
                    bm25_hits = _fulltext_search(q, top_k=search_limit, source_filters=source_filters)
                    results   = rrf_merge(vec_hits, bm25_hits, k=60)
                else:
                    results = vec_hits

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

                sys.stderr.write(
                    f"  - Query {idx}: '{q[:50]}' -> {count_valid} new results.\n"
                )

        # Persist rejected count for UI display
        try:
            st.session_state.rejected_count = rejected_count
        except Exception:
            pass

        # ── Final sort ────────────────────────────────────────────────────────
        all_results.sort(key=lambda x: x.get("_rrf_score", x["score"]), reverse=True)

        # ── 7. Reranker or plain top‑k ────────────────────────────────────────
        if use_reranker:
            final_results = rerank(query_text, all_results, top_k)
        else:
            for item in all_results:
                item["score_type"] = "rrf" if use_hybrid else "cosine"
            final_results = all_results[:top_k]

        # ── 8. Context Enrichment (skeleton retrieval) ────────────────────────
        enriched_results = list(final_results)
        parent_puids     = {r["parent_puid"] for r in final_results if r.get("parent_puid")}
        puids_to_fetch   = [p for p in parent_puids if p not in seen_puids]

        if puids_to_fetch:
            skeletons = fetch_nodes(puids_to_fetch, is_skeleton=True)
            for skel in skeletons:
                skel["score"]      = 0.99
                skel["score_type"] = "skeleton"
                enriched_results.append(skel)
                seen_puids.add(skel["puid"])

        return [
            Document(
                page_content=r["text"],
                metadata={
                    "filename":    r["filename"],
                    "lang":        r["lang"],
                    "score":       r.get("_rerank_score", r.get("_rrf_score", r["score"])),
                    "score_type":  r.get("score_type", "cosine_or_rrf"),
                    "start_line":  r["start_line"],
                    "end_line":    r["end_line"],
                    "is_test":     r["is_test"],
                    "node_type":   r.get("node_type", ""),
                    "node_name":   r.get("node_name", ""),
                    "qualified_name": r.get("qualified_name", ""),
                    "signature":   r.get("signature", ""),
                    "docstring":   r.get("docstring", ""),
                    "modifiers":   r.get("modifiers", ""),
                    "export_status": r.get("export_status", ""),
                    "source_span": r.get("source_span", ""),
                    "puid":        r.get("puid", ""),
                    "parent_puid": r.get("parent_puid", ""),
                    "is_skeleton": r.get("is_skeleton", False),
                    "repo_name":   r.get("repo_name", ""),
                },
            )
            for r in enriched_results
        ]

    except Exception as e:
        if "does not exist" in str(e).lower():
            return []
        st.error(f"Lỗi khi query CocoIndex: {e}")
        import logging
        logging.getLogger(__name__).error(f"Search Error: {e}", exc_info=True)
        return []


def get_llm(llm_choice, model_name="gemma3:4b", api_key=None, ollama_host=None):
    if llm_choice == "Ollama":
        url = ollama_host or os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
        return OllamaLLM(model=model_name, base_url=url)
    elif llm_choice in ("OpenAI", "Gemini"):
        raise NotImplementedError(
            f"{llm_choice} chưa được hỗ trợ. "
            "Cài langchain-openai hoặc langchain-google-genai vào requirements.txt."
        )
    else:
        raise ValueError(f"Unknown LLM choice: {llm_choice}")


def generate_answer_stream(query_text: str, docs: list[Document], llm):
    """
    Stream câu trả lời từ LLM dựa trên context từ pgvector search.
    Prompt được thiết kế để:
    - Chỉ rõ tên file + số dòng để dễ tra cứu
    - Thừa nhận khi không đủ thông tin
    """
    prompt_template = """\
Bạn là một kỹ sư phần mềm cấp cao đang hỗ trợ phân tích mã nguồn.
Dưới đây là các đoạn mã liên quan được trích xuất từ codebase (sắp xếp theo mức độ liên quan giảm dần):

<context>
{context}
</context>

Hướng dẫn trả lời:
1. Phân tích kỹ nội dung code để trả lời câu hỏi. 
2. Khi trích dẫn code, ghi rõ tên file và số dòng (nếu có)
3. Nếu nhiều file cùng có logic liên quan, liệt kê tất cả
4. Nếu context không đủ để trả lời chắc chắn, hãy nói rõ và gợi ý nơi tìm thêm
5. Trả lời bằng tiếng Việt nếu câu hỏi bằng tiếng Việt

Câu hỏi: {question}

Phân tích và trả lời bằng tiếng Việt hoặc tiếng Anh:"""

    prompt = PromptTemplate.from_template(prompt_template)

    # Build context với file location rõ ràng
    context_parts = []
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
            tag += " [SKELETON/MỤC LỤC]"

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

    context_str = "\n\n".join(context_parts)
    chain = prompt | llm

    for chunk in chain.stream({"context": context_str, "question": query_text}):
        if hasattr(chunk, "content"):
            yield chunk.content
        else:
            yield str(chunk)
