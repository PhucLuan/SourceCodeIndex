"""
Streamlit UI — Source Code Indexer & Chat

Cải tiến:
- sync_workspace: copy file thực thay vì symlink (symlink dễ bị cocoindex walker bỏ qua)
  → đảm bảo walk đủ mọi thư mục con
- Toggle "Ưu tiên logic (loại test files)" cho search
- Hiển thị file location (start_line/end_line) trong citations
- Top-K configurable
"""

import streamlit as st
import json
import os
import shutil
import asyncio
import asyncpg

from indexer_flow import app as coco_app
from rag import query_cocoindex_db, get_llm, generate_answer_stream

st.set_page_config(page_title="Source Code Indexer", page_icon="🔍", layout="wide")

SOURCES_FILE = "sources.json"
WORKSPACE_DIR = "/tmp/workspace"


def load_sources() -> list[str]:
    if not os.path.exists(SOURCES_FILE):
        return []
    with open(SOURCES_FILE) as f:
        return json.load(f)


def save_sources(sources: list[str]) -> None:
    with open(SOURCES_FILE, "w") as f:
        json.dump(sources, f)


def _map_windows_path(original_src: str) -> str:
    """Map đường dẫn Windows sang cấu trúc Docker mount."""
    src = original_src.replace("\\", "/")
    if src.lower().startswith("c:/"):
        src = "/host_c/" + src[3:]
    elif len(src) >= 2 and src[1] == ":" and src[2] == "/":
        drive = src[0].lower()
        src = f"/host_{drive}/" + src[3:]
    return src


def sync_workspace(includes_list: list[str], excludes_list: list[str]) -> None:
    """
    Copy source files vào WORKSPACE_DIR để cocoindex walk.

    Dùng shutil.copy2 thay vì symlink để đảm bảo:
    - Cocoindex walker đọc được tất cả thư mục con (recursive=True hoạt động đúng)
    - Không bị lỗi broken symlink khi đường dẫn Windows mount
    """
    sources = load_sources()
    if os.path.exists(WORKSPACE_DIR):
        shutil.rmtree(WORKSPACE_DIR)
    os.makedirs(WORKSPACE_DIR, exist_ok=True)

    excludes_set = {x.lower() for x in excludes_list}

    for original_src in sources:
        src = _map_windows_path(original_src)

        if not os.path.exists(src):
            st.warning(
                f"⚠️ Không tìm thấy: `{src}` "
                f"(gốc: `{original_src}`). Đảm bảo đã mount ổ đĩa."
            )
            continue

        basename = os.path.basename(src.rstrip("/"))
        dst_root = os.path.join(WORKSPACE_DIR, basename)

        if os.path.isdir(src):
            file_count = 0
            for root, dirs, files in os.walk(src, followlinks=True):
                # Lọc thư mục loại trừ — chỉnh dirs IN-PLACE để os.walk không đi vào
                dirs[:] = [
                    d for d in dirs
                    if d.lower() not in excludes_set and not d.startswith(".")
                ]

                rel_root = os.path.relpath(root, src)
                dst_dir = os.path.join(dst_root, rel_root) if rel_root != "." else dst_root
                os.makedirs(dst_dir, exist_ok=True)

                for filename in files:
                    ext = os.path.splitext(filename)[1].lower()
                    # Bỏ qua nếu includes_list chỉ định và ext không khớp
                    if includes_list and ext not in includes_list:
                        continue

                    src_file = os.path.join(root, filename)
                    dst_file = os.path.join(dst_dir, filename)
                    try:
                        shutil.copy2(src_file, dst_file)
                        file_count += 1
                    except Exception as copy_err:
                        st.warning(f"Không thể copy `{src_file}`: {copy_err}")

            st.info(f"✅ Đã sync {file_count} files từ `{original_src}`")

        elif os.path.isfile(src):
            os.makedirs(dst_root, exist_ok=True)
            try:
                shutil.copy2(src, os.path.join(dst_root, basename))
                st.info(f"✅ Đã sync file `{original_src}`")
            except Exception as e:
                st.warning(f"Không thể copy file `{src}`: {e}")


# ─── SIDEBAR ─────────────────────────────────────────────────────────────────

with st.sidebar:
    st.header("⚙️ Cấu hình Hệ thống")

    # --- Sources ---
    st.subheader("1. Nguồn Dữ liệu (Source)")
    st.info(
        "Nhập đường dẫn tuyệt đối của Source Code. "
        "Với ổ đĩa Windows được mount, dùng `/host_c/...` "
        "(ví dụ: `/host_c/LEARN/MyProject`)."
    )

    new_src = st.text_input("Đường dẫn Source Code:")
    if st.button("➕ Thêm Source"):
        if new_src.strip():
            srcs = load_sources()
            if new_src.strip() not in srcs:
                srcs.append(new_src.strip())
                save_sources(srcs)
                st.success(f"Đã thêm: {new_src.strip()}")
            else:
                st.warning("Source đã tồn tại.")

    st.write("---")
    st.write("**Danh sách source đang theo dõi:**")
    sources = load_sources()
    for s in sources:
        col1, col2 = st.columns([8, 2])
        col1.write(f"- `{s}`")
        if col2.button("✖", key=f"del_{s}"):
            sources.remove(s)
            save_sources(sources)
            st.rerun()

    # --- File Filtering ---
    st.write("---")
    st.subheader("Lọc File & Thư mục")
    file_extensions = st.text_input(
        "Đuôi file cần Index (phẩy phân cách, 'all' = tất cả):",
        value="all",
        help="Ví dụ: .cs, .py, .ts — để 'all' để index mọi loại file"
    )
    exclude_folders = st.text_input(
        "Thư mục loại trừ (phẩy phân cách):",
        value="bin, obj, .vs, node_modules, .git, dist, build, __pycache__",
    )

    # --- Index Actions ---
    st.write("---")
    col1, col2 = st.columns(2)
    with col1:
        btn_update = st.button("🔄 Cập nhật Index", use_container_width=True, type="primary")
    with col2:
        btn_reindex = st.button("🗑️ Index lại (Reset)", use_container_width=True)

    if btn_update or btn_reindex:
        with st.spinner("Đang chạy Indexing (có thể mất vài phút lần đầu)..."):
            includes_list = [
                x.strip().lower() for x in file_extensions.split(",") if x.strip()
            ]
            if "all" in includes_list:
                includes_list = []
            else:
                includes_list = [
                    x if x.startswith(".") else f".{x}" for x in includes_list
                ]

            excludes_list = [x.strip() for x in exclude_folders.split(",") if x.strip()]

            sync_workspace(includes_list, excludes_list)

            try:
                async def _run_update():
                    if btn_reindex:
                        # 1. Ép xóa table bằng SQL thay vì dựa vào coco_app.drop() (tránh lỗi cache schema)
                        db_url = os.environ.get(
                            "COCOINDEX_DATABASE_URL", 
                            "postgresql://cocoindex:cocoindex_password@localhost:5432/cocoindex_db"
                        )
                        try:
                            conn = await asyncpg.connect(db_url)
                            await conn.execute('DROP TABLE IF EXISTS "public"."code_embeddings" CASCADE')
                            await conn.close()
                        except Exception as e:
                            pass
                            
                        # 2. Vẫn gọi drop của cocoindex để dọn dẹp nội bộ
                        try:
                            await coco_app.drop()
                        except Exception:
                            pass
                            
                        # 3. Xoá DB local file/thư mục
                        db_path = os.environ.get("COCOINDEX_DB", "/app/cocoindex.db")
                        if os.path.exists(db_path):
                            try:
                                if os.path.isdir(db_path):
                                    shutil.rmtree(db_path)
                                else:
                                    os.remove(db_path)
                            except Exception as db_err:
                                st.warning(f"Không thể xoá DB cache cũ: {db_err}")
                                
                    await coco_app.update()

                asyncio.run(_run_update())

                if btn_reindex:
                    st.success("✅ Đã xoá DB cũ và tạo lại Index thành công!")
                else:
                    st.success("✅ Đã cập nhật Index thành công!")
            except Exception as e:
                st.error(f"❌ Lỗi index: {e}")

    # --- LLM Config ---
    st.write("---")
    st.subheader("2. Cấu hình LLM")
    llm_choice = st.selectbox("Chọn mô hình:", ["Ollama", "OpenAI", "Gemini"])

    model_name = "qwen2.5:32b"
    api_key = ""
    ollama_host = os.environ.get("OLLAMA_BASE_URL", "http://host.docker.internal:11434")

    if llm_choice == "Ollama":
        model_name = st.text_input("Tên model Ollama:", value="qwen2.5:32b")
        ollama_host = st.text_input("Ollama Host API:", value=ollama_host)
    else:
        api_key = st.text_input(f"{llm_choice} API Key:", type="password")

    # --- Search Config ---
    st.write("---")
    st.subheader("3. Tuỳ chỉnh Tìm kiếm")
    top_k = st.slider("Số context chunks:", min_value=3, max_value=20, value=8)

    # --- Source Filter ---
    st.write("---")
    st.subheader("4. Lọc theo Project")
    all_sources = load_sources()
    selected_sources = st.multiselect(
        "Chỉ tìm trong các nguồn này:",
        options=all_sources,
        default=None,
        help="Để trống để tìm kiếm trong toàn bộ các nguồn đã index."
    )
    # Map sang Docker path để query DB
    mapped_filters = [_map_windows_path(s) for s in selected_sources] if selected_sources else None


# ─── MAIN UI ─────────────────────────────────────────────────────────────────

st.title("🔍 Source Code Indexer & Chat")
st.markdown(
    "Tìm kiếm semantic qua **pgvector** · Trả lời thông minh bằng **RAG**"
)

if "messages" not in st.session_state:
    st.session_state.messages = []

for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

if query := st.chat_input("Nhập câu hỏi về codebase..."):
    st.session_state.messages.append({"role": "user", "content": query})
    with st.chat_message("user"):
        st.markdown(query)

    with st.chat_message("assistant"):        # 1. Search
        with st.spinner("Đang tìm kiếm ngữ cảnh..."):
            docs = query_cocoindex_db(
                query, 
                top_k=top_k,
                source_filters=mapped_filters
            )

            if not docs:
                response = "Không tìm thấy thông tin phù hợp. Đảm bảo đã Index source code."
                st.markdown(response)
                st.session_state.messages.append({"role": "assistant", "content": response})
            else:
                try:
                    llm = get_llm(
                        llm_choice,
                        model_name=model_name,
                        api_key=api_key,
                        ollama_host=ollama_host,
                    )
                    stream_gen = generate_answer_stream(query, docs, llm)
                    response = st.write_stream(stream_gen)
                    st.session_state.messages.append({"role": "assistant", "content": response})

                    with st.expander(f"📎 {len(docs)} nguồn trích dẫn"):
                        for idx, d in enumerate(docs):
                            meta = d.metadata
                            filename = meta.get("filename", "?")
                            start = meta.get("start_line", "?")
                            end = meta.get("end_line", "?")
                            score = meta.get("score", 0)
                            is_test = meta.get("is_test", False)
                            is_skeleton = meta.get("is_skeleton", False)
                            puid = meta.get("puid", "")
                            node_type = meta.get("node_type", "")
                            node_name = meta.get("node_name", "")
                            
                            tag = ""
                            if is_test: tag += " 🧪 TEST"
                            if is_skeleton: tag += " 📖 SKELETON"

                            node_info = f" **[{node_type.upper()}: {node_name}]**" if node_type and node_name else ""
                            st.write(
                                f"**[{idx+1}]** `{filename}`{node_info}  "
                                f"L{start}–L{end}{tag}  *(score: {score:.3f})*"
                            )
                            if puid:
                                st.caption(f"PUID: `{puid}`")
                            st.code(
                                d.page_content,
                                language=meta.get("lang", ""),
                            )

                except Exception as e:
                    st.error(f"❌ Lỗi LLM: {e}")
