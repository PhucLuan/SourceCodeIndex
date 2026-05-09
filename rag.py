"""
RAG layer — kết nối CocoIndex search với LangChain LLM.

Cải tiến:
- query_cocoindex_db: Sử dụng pure semantic search để phân biệt logic/test tự nhiên
- generate_answer_stream: Có prompt chuyên về code search + hướng dẫn LLM
"""

import os
from langchain_core.documents import Document
from langchain_ollama import OllamaLLM
from langchain_core.prompts import PromptTemplate

import streamlit as st

from indexer_flow import search as _search


def query_cocoindex_db(
    query_text: str,
    top_k: int = 8,
) -> list[Document]:
    """
    Semantic search dùng indexer_flow.search() với pgvector cosine similarity.
    Dựa vào AST-enriched embeddings để tự động phân biệt logic/test.
    """
    try:
        results = _search(query_text, top_k=top_k)
        return [
            Document(
                page_content=r["text"],
                metadata={
                    "filename":   r["filename"],
                    "lang":       r["lang"],
                    "score":      r["score"],
                    "start_line": r["start_line"],
                    "end_line":   r["end_line"],
                    "is_test":    r["is_test"],
                    "node_type":  r.get("node_type", ""),
                    "node_name":  r.get("node_name", ""),
                },
            )
            for r in results
        ]
    except Exception as e:
        st.error(f"Lỗi khi query CocoIndex: {e}")
        return []


def get_llm(llm_choice, model_name="qwen2.5:32b", api_key=None, ollama_host=None):
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

Phân tích và trả lời:"""

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
        tag = " [TEST FILE]" if is_test else ""

        node_info = f" [{node_type.upper()}: {node_name}]" if node_type and node_name else ""

        context_parts.append(
            f"--- {filename}:L{start}-L{end}{node_info}{tag} (relevance: {score:.3f}) ---\n"
            f"{d.page_content}"
        )

    context_str = "\n\n".join(context_parts)
    chain = prompt | llm

    for chunk in chain.stream({"context": context_str, "question": query_text}):
        if hasattr(chunk, "content"):
            yield chunk.content
        else:
            yield str(chunk)
