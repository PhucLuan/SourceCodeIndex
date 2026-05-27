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

from indexer_flow import search as _search, fetch_nodes, fulltext_search as _fulltext_search, rrf_merge


SCORE_THRESHOLD = 0.3

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

def query_cocoindex_db(
    query_text: str,
    top_k: int = 8,
    source_filters: list[str] = None,
    llm = None,
    similarity_threshold: float = 0.3,
    use_query_expansion: bool = True,
    use_hybrid: bool = True,
) -> list[Document]:
    """
    Semantic search nâng cao:
    1. Query Expansion (nếu có LLM và được bật)
    2. Over-fetch (top_k * 2) cho Vector và Full-Text
    3. Hybrid Merge qua Reciprocal Rank Fusion (nếu bật)
    4. Score Threshold filtering (dynamic)
    5. Loại bỏ trùng lặp
    """
    try:
        # 1. Query Expansion
        queries = expand_query(query_text, llm) if (llm and use_query_expansion) else [query_text]
        
        all_results = []
        seen_puids = set()
        
        # 2. Over-fetch cho mỗi query (lấy dư để filter)
        search_limit = top_k * 2
        rejected_count = 0
        
        sys.stderr.write(f"\n[RETRIEVAL_START] Processing {len(queries)} queries...\n")
        for idx, q in enumerate(queries):
            # Lấy vector
            vector_results = _search(q, top_k=search_limit, source_filters=source_filters)
            
            # Nếu bật hybrid, lấy cả full-text và merge
            if use_hybrid:
                bm25_results = _fulltext_search(q, top_k=search_limit, source_filters=source_filters)
                results = rrf_merge(vector_results, bm25_results, k=60)
            else:
                results = vector_results
                
            count_valid = 0
            for r in results:
                # 3. Score Threshold (Cosine Similarity hoặc RRF score tuỳ loại search, tạm bỏ qua threshold cho RRF nếu cần)
                score = r.get("_rrf_score", r["score"])
                if not use_hybrid and score < similarity_threshold:
                    rejected_count += 1
                    continue
                
                # 4. De-duplicate by PUID
                puid = r.get("puid", "")
                if puid not in seen_puids:
                    all_results.append(r)
                    seen_puids.add(puid)
                    count_valid += 1
            sys.stderr.write(f"  - Query {idx}: '{q[:50]}...' -> Found {len(results)} total, {count_valid} new above threshold.\n")
        
        # Lưu số lượng bị loại vào session state để hiển thị trên UI
        st.session_state.rejected_count = rejected_count
        
        # Sắp xếp lại theo score giảm dần sau khi gộp
        # Nếu dùng hybrid thì score chính là _rrf_score, nếu không là score gốc
        all_results.sort(key=lambda x: x.get("_rrf_score", x["score"]), reverse=True)
        
        # Lấy lại đúng số lượng top_k yêu cầu
        final_results = all_results[:top_k]
        
        # --- Giai đoạn 5: Context Enrichment (Skeleton Retrieval) ---
        # Nếu kết quả là method/function, ta lấy thêm Skeleton của Class cha hoặc File
        enriched_results = list(final_results)
        parent_puids = {r["parent_puid"] for r in final_results if r.get("parent_puid")}
        
        # Lọc các PUID chưa có trong kết quả hiện tại
        puids_to_fetch = [p for p in parent_puids if p not in seen_puids]
        
        if puids_to_fetch:
            skeletons = fetch_nodes(puids_to_fetch, is_skeleton=True)
            for skel in skeletons:
                # Gán score cố định để đánh dấu đây là context bổ trợ
                skel["score"] = 0.99 
                enriched_results.append(skel)
                seen_puids.add(skel["puid"])
        
        return [
            Document(
                page_content=r["text"],
                metadata={
                    "filename":   r["filename"],
                    "lang":       r["lang"],
                    "score":      r.get("_rrf_score", r["score"]),
                    "start_line": r["start_line"],
                    "end_line":   r["end_line"],
                    "is_test":    r["is_test"],
                    "node_type":  r.get("node_type", ""),
                    "node_name":  r.get("node_name", ""),
                    "puid":       r.get("puid", ""),
                    "parent_puid": r.get("parent_puid", ""),
                    "is_skeleton": r.get("is_skeleton", False),
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
        puid = meta.get("puid", "")
        is_skeleton = meta.get("is_skeleton", False)
        
        tag = " [TEST FILE]" if is_test else ""
        if is_skeleton:
            tag += " [SKELETON/MỤC LỤC]"

        node_info = f" [{node_type.upper()}: {node_name}]" if node_type and node_name else ""
        puid_info = f"\nPUID: {puid}" if puid else ""

        context_parts.append(
            f"--- {filename}:L{start}-L{end}{node_info}{tag}{puid_info} (relevance: {score:.3f}) ---\n"
            f"{d.page_content}"
        )

    context_str = "\n\n".join(context_parts)
    chain = prompt | llm

    for chunk in chain.stream({"context": context_str, "question": query_text}):
        if hasattr(chunk, "content"):
            yield chunk.content
        else:
            yield str(chunk)
