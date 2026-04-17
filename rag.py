import os
import psycopg2
from sentence_transformers import SentenceTransformer
from langchain_core.documents import Document
from langchain_community.llms.ollama import Ollama
from langchain_openai import ChatOpenAI
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.prompts import PromptTemplate

import streamlit as st

@st.cache_resource
def get_embedding_model():
    return SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")

def query_cocoindex_db(query_text, top_k=5):
    """
    Search vector similarity against the table populated by CocoIndex.
    """
    db_uri = os.environ.get("COCOINDEX_DATABASE_URL", "postgresql://cocoindex:cocoindex_password@localhost:5432/cocoindex_db")
    
    # 1. Generate query embedding
    embedding_model = get_embedding_model()
    query_vec = embedding_model.encode(query_text).tolist()
    
    conn = None
    try:
        # 2. Connect to Database
        conn = psycopg2.connect(db_uri)
        cur = conn.cursor()
        
        # 3. Query pgvector using L2 distance <-> operator
        cur.execute(
            """
            SELECT filename, lang, text 
            FROM code_embeddings_table 
            ORDER BY embedding <=> %s::vector 
            LIMIT %s;
            """,
            (query_vec, top_k)
        )
        rows = cur.fetchall()
        
        docs = []
        for row in rows:
            filename, lang, text = row
            docs.append(Document(
                page_content=text,
                metadata={"filename": filename, "lang": lang}
            ))
        return docs
    except Exception as e:
        print(f"Error querying db: {e}")
        return []
    finally:
        if conn:
            cur.close()
            conn.close()

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
    
    # Gộp context thành một string
    context_str = "\n\n".join([f"--- File: {d.metadata.get('filename')} ---\n{d.page_content}" for d in docs])
    
    chain = prompt | llm
    
    for chunk in chain.stream({"context": context_str, "question": query_text}):
        if hasattr(chunk, "content"):
            yield chunk.content
        else:
            yield str(chunk)
