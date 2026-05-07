import os
from langchain_core.documents import Document
from langchain_community.llms.ollama import Ollama
from langchain_openai import ChatOpenAI
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.prompts import PromptTemplate

import cocoindex
import streamlit as st

# Import search handler và flow đã được đăng ký trong indexer_flow.
# CocoIndex sẽ init và quản lý connection pool bên trong.
from indexer_flow import search as _cocoindex_search


def query_cocoindex_db(query_text: str, top_k: int = 5) -> list[Document]:
    """
    Semantic search dùng built-in CocoIndex query handler.
    Không cần viết SQL hay quản lý embedding model thủ công.
    """
    try:
        # Gọi thẳng CocoIndex search handler đã khai báo với @code_embedding_flow.query_handler()
        # Hàm này tự embed query bằng code_to_embedding.eval() rồi query pgvector
        query_output = _cocoindex_search(query_text)

        docs = []
        for result in query_output.results[:top_k]:
            docs.append(Document(
                page_content=result["text"],
                metadata={
                    "filename": result["filename"],
                    "lang":     result["lang"],
                    "score":    result["score"],
                }
            ))
        return docs

    except Exception as e:
        st.error(f"Lỗi khi query CocoIndex: {e}")
        return []


def get_llm(llm_choice, model_name="llama-local", api_key=None, ollama_host=None):
    if llm_choice == "Ollama":
        url = ollama_host or os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
        return Ollama(model=model_name, base_url=url)
    elif llm_choice == "OpenAI":
        return ChatOpenAI(model="gpt-4o", openai_api_key=api_key)
    elif llm_choice == "Gemini":
        return ChatGoogleGenerativeAI(model="gemini-1.5-pro", google_api_key=api_key)
    else:
        raise ValueError("Unknown LLM choice")


def generate_answer_stream(query_text, docs, llm):
    """
    Generate an answer using Streaming approach for smoother UI.
    """
    prompt_template = """
Bạn là một kỹ sư phần mềm cấp cao hỗ trợ trả lời các câu hỏi về chuyên môn code. Dưới đây là các đoạn mã nguồn và tài liệu liên quan được trích xuất từ codebase hiện tại:

<context>
{context}
</context>

Dựa vào các trích đoạn trên, hãy trả lời câu hỏi sau của người dùng một cách chi tiết và chính xác. 
Nếu có thể, hãy trích dẫn cụ thể tên file để người dùng dễ dàng theo dõi (tên file được cung cấp trên mỗi phần). Nếu bạn không tìm thấy câu trả lời, hãy nói là với context trên không đủ thông tin.

Câu hỏi: {question}
Chuyên gia trả lời:"""

    prompt = PromptTemplate.from_template(prompt_template)

    context_str = "\n\n".join([
        f"--- File: {d.metadata.get('filename')} (score: {d.metadata.get('score', 0):.3f}) ---\n{d.page_content}"
        for d in docs
    ])

    chain = prompt | llm

    for chunk in chain.stream({"context": context_str, "question": query_text}):
        if hasattr(chunk, "content"):
            yield chunk.content
        else:
            yield str(chunk)
