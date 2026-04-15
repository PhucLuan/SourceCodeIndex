import streamlit as st
import json
import os
import shutil
import subprocess
from rag import query_cocoindex_db, get_llm, generate_answer

st.set_page_config(page_title="Source Code Indexer", page_icon="🔍", layout="wide")

SOURCES_FILE = "sources.json"
WORKSPACE_DIR = "/tmp/workspace"

def load_sources():
    if not os.path.exists(SOURCES_FILE):
        return []
    with open(SOURCES_FILE) as f:
        return json.load(f)

def save_sources(sources):
    with open(SOURCES_FILE, "w") as f:
        json.dump(sources, f)

def sync_workspace(includes_list, excludes_list):
    sources = load_sources()
    if os.path.exists(WORKSPACE_DIR):
        shutil.rmtree(WORKSPACE_DIR)
    os.makedirs(WORKSPACE_DIR)
        
    for original_src in sources:
        # Tự động map đường dẫn Windows sang cấu trúc mount của Docker
        src = original_src.replace('\\', '/')
        if src.lower().startswith('c:/'):
            src = '/host_c/' + src[3:]
            
        if os.path.exists(src):
            basename = os.path.basename(src.rstrip("/"))
            dst_root = os.path.join(WORKSPACE_DIR, basename)
            
            if os.path.isdir(src):
                for root, dirs, files in os.walk(src):
                    # Lọc loại bỏ thư mục
                    if excludes_list:
                        dirs[:] = [d for d in dirs if d not in excludes_list]
                        
                    for file in files:
                        ext = os.path.splitext(file)[1].lower()
                        # Loại bỏ nếu include_list không trống và đuôi không khớp
                        if includes_list and ext not in includes_list:
                            continue
                            
                        file_src_path = os.path.join(root, file)
                        rel_path = os.path.relpath(file_src_path, src)
                        file_dst_path = os.path.join(dst_root, rel_path)
                        
                        os.makedirs(os.path.dirname(file_dst_path), exist_ok=True)
                        try:
                            os.symlink(file_src_path, file_dst_path, target_is_directory=False)
                        except:
                            pass
            else:
                # Nếu source thêm vào là 1 file độc lập
                os.makedirs(dst_root, exist_ok=True)
                try:
                    os.symlink(src, os.path.join(dst_root, basename), target_is_directory=False)
                except:
                    pass
        else:
            st.error(f"Không thể truy cập đường dẫn (kể cả sau đổi): {src}. Đảm bảo bạn đã Mount ổ đĩa đó.")

# --- SIDEBAR UI ---
with st.sidebar:
    st.header("⚙️ Cấu hình Hệ thống")
    
    st.subheader("1. Nguồn Dữ liệu (Source)")
    st.info("Nhập đường dẫn tuyệt đối của Source Code (Dùng /host_c/... cho các ổ đĩa của Windows được mount vào, ví dụ: /host_c/LEARN/SourceCodeIndex).")
    
    new_src = st.text_input("Đường dẫn Source Code:")
    if st.button("Thêm Source"):
        if new_src:
            srcs = load_sources()
            if new_src not in srcs:
                srcs.append(new_src)
                save_sources(srcs)
                st.success(f"Đã thêm: {new_src}")
                
    st.write("---")
    st.write("**Danh sách các source đang theo dõi:**")
    sources = load_sources()
    for s in sources:
        col1, col2 = st.columns([8, 2])
        col1.write(f"- `{s}`")
        if col2.button("X", key=f"del_{s}"):
            sources.remove(s)
            save_sources(sources)
            st.rerun()

    st.write("---")
    st.subheader("Cấu hình Lọc File & Thư mục")
    file_extensions = st.text_input("Đuôi file cần Index (cách nhau bởi dấu phẩy, chữ 'all' để lấy tất cả):", value="all")
    exclude_folders = st.text_input("Loại trừ các thư mục (cách nhau dấu phẩy):", value="bin, obj, .vs, node_modules, .git")
    
    st.write("---")
    col1, col2 = st.columns(2)
    with col1:
        btn_update = st.button("🔄 Cập nhật Index", use_container_width=True, type="primary")
    with col2:
        btn_reindex = st.button("🗑️ Index lại (Reset)", use_container_width=True)
        
    if btn_update or btn_reindex:
        with st.spinner("Đang chạy quá trình Indexing (có thể mất nhiều phút)..."):
            
            # Tiền xử lý inputs filtering
            includes_list = [x.strip().lower() for x in file_extensions.split(",") if x.strip()]
            if "all" in includes_list:
                includes_list = []
            else:
                # Đảm bảo có bắt đầu bằng dấu chấm
                includes_list = [x if x.startswith('.') else f".{x}" for x in includes_list]
                
            excludes_list = [x.strip() for x in exclude_folders.split(",") if x.strip()]

            sync_workspace(includes_list, excludes_list)
            
            # Goi cocoindex update
            try:
                env = os.environ.copy()
                
                cmd_args = ["cocoindex", "update", "indexer_flow", "--force"]
                if btn_reindex:
                    cmd_args.append("--reset")
                    
                result = subprocess.run(
                    cmd_args,
                    capture_output=True, text=True, env=env
                )
                if result.returncode == 0:
                    if btn_reindex:
                        st.success("Đã xoá DB cũ và tạo lại Index thành công!")
                    else:
                        st.success("Cập nhật Index thành công!")
                else:
                    st.error(f"Lỗi index: {result.stderr}")
            except Exception as e:
                st.error(f"Không thể chạy cocoindex: {str(e)}")

    st.write("---")
    st.subheader("2. Cấu hình LLM")
    llm_choice = st.selectbox("Chọn mô hình:", ["Ollama", "OpenAI", "Gemini"])
    
    model_name = "llama-local"
    api_key = ""
    ollama_host = os.environ.get("OLLAMA_BASE_URL", "http://host.docker.internal:11434")
    
    if llm_choice == "Ollama":
        model_name = st.text_input("Tên model Ollama:", value="llama-local")
        ollama_host = st.text_input("Ollama Host API:", value=ollama_host)
    else:
        api_key = st.text_input(f"{llm_choice} API Key:", type="password")

# --- MAIN UI ---
st.title("Giao diện Tra cứu & Chat Mã nguồn")
st.markdown("Hỗ trợ tìm kiếm Semantic cực nhanh bằng **pgvector** và trả lời thông minh dựa trên logic RAG.")

if "messages" not in st.session_state:
    st.session_state.messages = []

# Display chat messages
for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

# Chat input
if query := st.chat_input("Nhập câu hỏi liên quan tới codebase..."):
    # Add user msg to state
    st.session_state.messages.append({"role": "user", "content": query})
    with st.chat_message("user"):
        st.markdown(query)
    
    # Generate bot msg
    with st.chat_message("assistant"):
        with st.spinner("Đang tìm kiếm thông tin và tổng hợp..."):
            docs = query_cocoindex_db(query, top_k=5)
            if not docs:
                response = "Không tìm thấy code nào khớp với yêu cầu hoặc DB chưa được index."
                st.markdown(response)
                st.session_state.messages.append({"role": "assistant", "content": response})
            else:
                try:
                    llm = get_llm(llm_choice, model_name=model_name, api_key=api_key, ollama_host=ollama_host)
                    result = generate_answer(query, docs, llm)
                    
                    # Usually chain invoke returns varying types, if it's standard BaseMessage it stringifies it
                    response = result.content if hasattr(result, "content") else str(result)
                    
                    st.markdown(response)
                    st.session_state.messages.append({"role": "assistant", "content": response})
                    
                    with st.expander("Nguồn trích dẫn (Context Used)"):
                        for idx, d in enumerate(docs):
                            st.write(f"**FILE {idx+1}:** `{d.metadata.get('filename')}`")
                            st.code(d.page_content, language=d.metadata.get('lang', ''))
                except Exception as e:
                    st.error(f"Lỗi khi invoke cấu hình LLM: {str(e)}")
