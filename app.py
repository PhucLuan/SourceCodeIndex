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
import pandas as pd
import pathlib

from indexer_flow import app as coco_app
from rag import query_cocoindex_db, get_llm, generate_answer_stream
from embedder_config import load_active_profile, save_active_profile, DEFAULT_PROFILES

st.set_page_config(page_title="Source Code Indexer", page_icon="🔍", layout="wide")

SOURCES_FILE = "sources.json"
WORKSPACE_DIR = "/tmp/workspace"
PG_SCHEMA     = "public"

# Lấy động table_name từ active profile
_active_profile = load_active_profile()
TABLE_NAME    = _active_profile.table_name


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

def get_workspace_subdir(original_src: str) -> str:
    """Tạo tên thư mục đích duy nhất trong workspace dựa trên hash đường dẫn chuẩn hóa."""
    import hashlib
    # Chuẩn hóa: chữ thường, gạch chéo xuôi, bỏ gạch chéo cuối
    normalized = original_src.replace("\\", "/").lower().rstrip("/")
    path_hash = hashlib.md5(normalized.encode()).hexdigest()[:6]
    folder_name = os.path.basename(normalized)
    return f"{folder_name}_{path_hash}"


def load_repo_chunk_counts() -> dict[str, int]:
    """Truy vấn SQL gom nhóm theo repo_name để lấy thống kê số chunk đã index."""
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    
    if loop.is_closed():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
    async def _fetch():
        from embedder_config import load_active_profile
        act_prof = load_active_profile()
        db_url = os.environ.get(
            "COCOINDEX_DATABASE_URL", 
            "postgresql://cocoindex:cocoindex_password@localhost:5432/cocoindex_db"
        )
        try:
            conn = await asyncpg.connect(db_url)
            rows = await conn.fetch(
                f'SELECT repo_name, COUNT(*) as cnt FROM "{PG_SCHEMA}"."{act_prof.table_name}" GROUP BY repo_name'
            )
            await conn.close()
            return {r["repo_name"] or "Không rõ": r["cnt"] for r in rows}
        except Exception:
            return {}
            
    return loop.run_until_complete(_fetch())


def sync_workspace(sources_to_sync: list[str], includes_list: list[str], excludes_list: list[str]) -> None:
    """
    Copy source files vào WORKSPACE_DIR.
    """
    os.makedirs(WORKSPACE_DIR, exist_ok=True)
    
    # Lấy danh sách các thư mục đích hợp lệ hiện tại
    valid_dst_names = [get_workspace_subdir(s) for s in sources_to_sync]
    
    # Dọn dẹp các project KHÔNG được chọn khỏi workspace để tránh index nhầm
    for item in os.listdir(WORKSPACE_DIR):
        item_path = os.path.join(WORKSPACE_DIR, item)
        if os.path.isdir(item_path) and item not in valid_dst_names:
            shutil.rmtree(item_path)

    excludes_set = {x.lower() for x in excludes_list}

    import hashlib
    for original_src in sources_to_sync:
        src = _map_windows_path(original_src)

        if not os.path.exists(src):
            st.warning(f"⚠️ Không tìm thấy: `{src}`")
            continue

        dst_name = get_workspace_subdir(original_src)
        dst_root = os.path.join(WORKSPACE_DIR, dst_name)

        if os.path.exists(dst_root):
            shutil.rmtree(dst_root)
        os.makedirs(dst_root, exist_ok=True)

        if os.path.isdir(src):
            file_count = 0
            for root, dirs, files in os.walk(src, followlinks=True):
                dirs[:] = [d for d in dirs if d.lower() not in excludes_set and not d.startswith(".")]
                rel_root = os.path.relpath(root, src)
                dst_dir = os.path.join(dst_root, rel_root) if rel_root != "." else dst_root
                os.makedirs(dst_dir, exist_ok=True)

                for filename in files:
                    ext = os.path.splitext(filename)[1].lower()
                    if includes_list and ext not in includes_list:
                        continue
                    src_file = os.path.join(root, filename)
                    dst_file = os.path.join(dst_dir, filename)
                    shutil.copy2(src_file, dst_file)
                    file_count += 1
            st.info(f"✅ Đã sync {file_count} files từ `{original_src}` vào `{dst_name}`")
        elif os.path.isfile(src):
            shutil.copy2(src, os.path.join(dst_root, os.path.basename(src)))
            st.info(f"✅ Đã sync file `{original_src}`")


# ─── SIDEBAR ─────────────────────────────────────────────────────────────────

with st.sidebar:
    st.header("⚙️ Cấu hình Hệ thống")

    # --- Sources ---
    st.subheader("1. Nguồn Dữ liệu (Source)")
    sources = load_sources()
    
    # Sử dụng st.data_editor để quản lý danh sách nguồn linh hoạt hơn
    if "df_sources" not in st.session_state:
        st.session_state.df_sources = pd.DataFrame([{"Source Path": s, "Select": True} for s in sources])

    # Kiểm tra xem có sự thay đổi giữa load_sources và df_sources không
    current_source_paths = st.session_state.df_sources["Source Path"].tolist()
    if set(current_source_paths) != set(sources):
        # Đồng bộ lại nếu file json thay đổi (vd: thêm mới)
        new_df = pd.DataFrame([{"Source Path": s, "Select": True} for s in sources])
        st.session_state.df_sources = new_df

    edited_df = st.data_editor(
        st.session_state.df_sources,
        column_config={
            "Select": st.column_config.CheckboxColumn(default=True),
            "Source Path": st.column_config.TextColumn(width="large")
        },
        num_rows="dynamic",
        hide_index=True,
        key="sources_editor"
    )
    
    # Lưu lại nếu người dùng xóa/thêm dòng trong editor
    updated_sources = [s for s in edited_df["Source Path"].tolist() if s and s.strip()]
    if updated_sources != sources:
        save_sources(updated_sources)
        st.session_state.df_sources = edited_df

    active_sources = edited_df[edited_df["Select"] == True]["Source Path"].tolist()
    st.info(f"Đang chọn: {len(active_sources)} nguồn")

    # Thêm source mới
    new_src = st.text_input("Thêm đường dẫn mới:")
    if st.button("➕ Thêm"):
        if new_src.strip():
            srcs = load_sources()
            if new_src.strip() not in srcs:
                srcs.append(new_src.strip())
                save_sources(srcs)
                st.rerun()
            else:
                st.warning("Source đã tồn tại.")

    # --- Embedding Model ---
    st.write("---")
    st.subheader("1.5. Embedding Model (Bản đồ Vector)")
    
    act_prof = load_active_profile()
    profile_keys = list(DEFAULT_PROFILES.keys())
    profile_names = [DEFAULT_PROFILES[k].display_name for k in profile_keys]
    
    try:
        active_index = profile_keys.index(act_prof.name)
    except ValueError:
        active_index = 0
        
    selected_name = st.selectbox(
        "Chọn mô hình nhúng (Embedding):",
        options=profile_names,
        index=active_index,
        help="Đổi mô hình yêu cầu phải chạy 'Reset Selected' hoặc re-index lại từ đầu để tránh lỗi mismatch vector."
    )
    
    selected_key = profile_keys[profile_names.index(selected_name)]
    selected_profile = DEFAULT_PROFILES[selected_key]
    
    st.caption(f"**Model ID:** `{selected_profile.model_id}`")
    st.caption(f"**Bảng DB:** `{selected_profile.table_name}` · **Context:** {selected_profile.max_tokens} tokens")
    st.info(selected_profile.description)
    
    if selected_profile.name != act_prof.name:
        st.warning("⚠️ Bạn đã thay đổi Embedding Model! Bạn bắt buộc phải chạy 'Reset Selected' để xóa và nhúng lại mã nguồn bằng model mới.")
        if st.button("🔄 Xác nhận & Lưu cấu hình model", use_container_width=True):
            save_active_profile(selected_profile)
            st.success("Đã lưu cấu hình mới! Hãy nhấn nút Reset bên dưới để re-index.")
            st.rerun()

    # --- File Filtering ---
    st.write("---")
    st.subheader("Lọc File & Thư mục")
    file_extensions = st.text_input(
        "Đuôi file cần Index (phẩy phân cách, 'all' = tất cả):",
        value="all",
        help="Ví dụ: .cs, .py, .ts, .html, .css, .scss — để 'all' để index mọi loại file"
    )
    exclude_folders = st.text_input(
        "Thư mục loại trừ (phẩy phân cách):",
        value="bin, obj, .vs, node_modules, .git, dist, build, __pycache__, .vscode, .idea",
    )

    # --- Index Actions ---
    st.write("---")
    col1, col2 = st.columns(2)
    with col1:
        btn_update = st.button("🚀 Cập nhật Selected", use_container_width=True, type="primary")
    with col2:
        btn_reindex = st.button("🗑️ Reset Selected", use_container_width=True, help="Xóa và Index lại từ đầu cho các nguồn được chọn")

    if btn_update or btn_reindex:
        if not active_sources:
            st.warning("Vui lòng chọn ít nhất một nguồn để xử lý.")
        else:
            with st.spinner("Đang chuẩn bị dữ liệu..."):
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

                sync_workspace(active_sources, includes_list, excludes_list)

                try:
                    async def _run_partial_update():
                        db_url = os.environ.get(
                            "COCOINDEX_DATABASE_URL", 
                            "postgresql://cocoindex:cocoindex_password@localhost:5432/cocoindex_db"
                        )
                        
                        if btn_reindex:
                            # RESET SELECTED: Xóa dữ liệu cũ của các nguồn được chọn
                            try:
                                conn = await asyncpg.connect(db_url)
                                from embedder_config import load_active_profile
                                act_prof = load_active_profile()
                                for src_path in active_sources:
                                    dst_name = get_workspace_subdir(src_path)
                                    db_prefix = f"{WORKSPACE_DIR}/{dst_name}/"
                                    st.write(f"Đang làm sạch: `{src_path}`...")
                                    try:
                                        await conn.execute(
                                            f'DELETE FROM "{PG_SCHEMA}"."{act_prof.table_name}" WHERE filename LIKE $1',
                                            f"{db_prefix}%"
                                        )
                                    except asyncpg.exceptions.UndefinedTableError:
                                        # Bảng chưa được khởi tạo, bỏ qua
                                        pass
                                await conn.close()
                                # Dọn dẹp cache cocoindex nếu cần
                                try:
                                    await coco_app.drop() # Vẫn drop cache nội bộ để đảm bảo crawl lại
                                except:
                                    pass
                            except Exception as e:
                                st.warning(f"Không thể xóa dữ liệu cũ: {e}")
                                
                        await coco_app.update()

                    # Quản lý loop an toàn cho Streamlit
                    try:
                        loop = asyncio.get_event_loop()
                    except RuntimeError:
                        loop = asyncio.new_event_loop()
                        asyncio.set_event_loop(loop)
                    
                    if loop.is_closed():
                        loop = asyncio.new_event_loop()
                        asyncio.set_event_loop(loop)
                        
                    loop.run_until_complete(_run_partial_update())

                    if btn_reindex:
                        st.success(f"✅ Đã Reset và Index lại {len(active_sources)} nguồn!")
                    else:
                        st.success(f"✅ Đã cập nhật {len(active_sources)} nguồn thành công!")
                except Exception as e:
                    st.error(f"❌ Lỗi index: {e}")

    # --- LLM Config ---
    st.write("---")
    st.subheader("2. Cấu hình LLM")
    llm_choice = st.selectbox("Chọn mô hình:", ["Ollama", "OpenAI", "Gemini"])

    model_name = "gemma3:4b"
    api_key = ""
    ollama_host = os.environ.get("OLLAMA_BASE_URL", "http://host.docker.internal:11434")

    if llm_choice == "Ollama":
        model_name = st.text_input("Tên model Ollama:", value="gemma3:4b")
        ollama_host = st.text_input("Ollama Host API:", value=ollama_host)
    else:
        api_key = st.text_input(f"{llm_choice} API Key:", type="password")

    # --- Search Config ---
    st.write("---")
    st.subheader("3. Tuỳ chỉnh Tìm kiếm")
    top_k = st.slider("Số context chunks:", min_value=3, max_value=20, value=8)
    similarity_threshold = st.slider(
        "Similarity Threshold (Ngưỡng lọc):",
        min_value=0.0,
        max_value=1.0,
        value=0.30,
        step=0.05,
        help="Loại bỏ các chunk có Cosine Similarity nhỏ hơn ngưỡng này để giảm thiểu noise."
    )
    use_query_expansion = st.checkbox(
        "Bật Query Expansion",
        value=True,
        help="Sử dụng LLM để tạo các biến thể câu hỏi (tăng khả năng tìm thấy code)."
    )
    hybrid_search = st.checkbox(
        "🔀 Hybrid Search (Vector + BM25)",
        value=True,
        help="Kết hợp tìm kiếm nhúng vector và tìm kiếm full-text BM25. Kết quả sẽ được hợp nhất bằng RRF."
    )
    use_reranker = st.checkbox(
        "🔥 Bật Cross-Encoder Reranker",
        value=False,
        help="Sắp xếp lại các đoạn code bằng mô hình Cross-Encoder để tăng độ chính xác của kết quả, tốn thêm 1.5 - 3s phản hồi."
    )


    # --- Source Filter ---
    st.write("---")
    st.subheader("4. Phạm vi Tìm kiếm (Scope)")
    search_scope = st.radio(
        "Chọn phạm vi tìm kiếm:",
        ["Tìm trên tất cả Project (Cross-Repo)", "Chỉ tìm trong Project được chọn"],
        index=0,
        help="Chọn 'Tìm trên tất cả Project' để tự động tìm kiếm chéo giữa mọi repository đã index."
    )
    
    search_sources_selection = []
    if search_scope == "Chỉ tìm trong Project được chọn":
        all_sources = load_sources()
        search_sources_selection = st.multiselect(
            "Chỉ tìm trong các nguồn này:",
            options=all_sources,
            default=None,
            key="chat_search_sources",
            help="Chọn một hoặc nhiều project để giới hạn phạm vi tìm kiếm."
        )

    # --- Index Stats ---
    st.write("---")
    st.subheader("📊 Thống kê Index")
    counts = load_repo_chunk_counts()
    if counts:
        for repo, cnt in counts.items():
            st.write(f"- 📦 **{repo}**: {cnt:,} chunks")
    else:
        st.caption("Chưa có dữ liệu index hoặc DB trống.")


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
    # KIỂM TRA: Bắt buộc chọn nguồn nếu ở chế độ lọc project
    if search_scope == "Chỉ tìm trong Project được chọn" and not search_sources_selection:
        st.error("⚠️ Vui lòng chọn ít nhất một Project trong mục **'4. Phạm vi Tìm kiếm (Scope)'** ở thanh bên trái trước khi tìm kiếm!")
    else:
        st.session_state.messages.append({"role": "user", "content": query})
        with st.chat_message("user"):
            st.markdown(query)

        with st.chat_message("assistant"):        # 1. Search
            with st.spinner("Đang tìm kiếm ngữ cảnh..."):
                try:
                    llm = get_llm(
                        llm_choice,
                        model_name=model_name,
                        api_key=api_key,
                        ollama_host=ollama_host,
                    )
                except Exception as e:
                    st.error(f"❌ Lỗi khởi tạo LLM: {e}")
                    st.stop()

                docs = query_cocoindex_db(
                    query, 
                    top_k=top_k,
                    source_filters=search_sources_selection if search_scope == "Chỉ tìm trong Project được chọn" else None,
                    llm=llm,
                    similarity_threshold=similarity_threshold,
                    use_query_expansion=use_query_expansion,
                    use_hybrid=hybrid_search,
                    use_reranker=use_reranker
                )

                if "rejected_count" in st.session_state and st.session_state.rejected_count > 0:
                    st.info(f"ℹ️ Đã lọc bỏ {st.session_state.rejected_count} chunks có độ tương đồng < {similarity_threshold:.2f}")

                if not docs:
                    response = "Không tìm thấy thông tin phù hợp trong các Project đã chọn. Đảm bảo bạn đã Index các project này hoặc giảm Similarity Threshold."
                    st.markdown(response)
                    st.session_state.messages.append({"role": "assistant", "content": response})
                else:
                    try:
                        stream_gen = generate_answer_stream(query, docs, llm)
                        response = st.write_stream(stream_gen)
                        st.session_state.messages.append({"role": "assistant", "content": response})

                        with st.expander(f"📎 {len(docs)} nguồn trích dẫn"):
                            for idx, d in enumerate(docs):
                                meta = d.metadata
                                filename = meta.get("filename", "?")
                                # Rút gọn đường dẫn hiển thị (bỏ /tmp/workspace/project_hash/)
                                display_filename = filename
                                if filename.startswith("/tmp/workspace/"):
                                    parts = filename.replace("/tmp/workspace/", "").split("/", 1)
                                    if len(parts) > 1:
                                        display_filename = parts[1]
                                    else:
                                        display_filename = parts[0]

                                repo_name = meta.get("repo_name", "")
                                repo_tag = f" 📦 [{repo_name}]" if repo_name else ""

                                start = meta.get("start_line", "?")
                                end = meta.get("end_line", "?")
                                score = meta.get("score", 0)
                                is_test = meta.get("is_test", False)
                                is_skeleton = meta.get("is_skeleton", False)
                                puid = meta.get("puid", "")
                                node_type = meta.get("node_type", "")
                                node_name = meta.get("node_name", "")
                                qualified_name = meta.get("qualified_name", "")
                                source_span = meta.get("source_span", "")
                                modifiers = meta.get("modifiers", "")
                                
                                tag = ""
                                if is_test: tag += " 🧪 TEST"
                                if is_skeleton: tag += " 📖 SKELETON"

                                score_type = meta.get("score_type", "cosine_or_rrf")

                                # Định dạng màu sắc/icon theo score và score_type
                                if score_type == "rerank":
                                    # Logit thô của Cross-Encoder (ms-marco-MiniLM-L-6-v2) thường nằm trong khoảng [-10, 10]
                                    if score > 0.0:
                                        score_icon = "🟢"
                                        score_desc = "High relevance (Reranked)"
                                    elif score > -2.0:
                                        score_icon = "🟡"
                                        score_desc = "Medium relevance (Reranked)"
                                    else:
                                        score_icon = "🔴"
                                        score_desc = "Low relevance (Reranked)"
                                    score_str = f"{score:+.3f}"
                                elif score_type == "skeleton":
                                    score_icon = "📖"
                                    score_desc = "Context Enrichment"
                                    score_str = "N/A"
                                else:
                                    if score >= 0.7:
                                        score_icon = "🟢"
                                        score_desc = "High relevance"
                                    elif score >= 0.5:
                                        score_icon = "🟡"
                                        score_desc = "Medium relevance"
                                    else:
                                        score_icon = "🔴"
                                        score_desc = "Low relevance"
                                    score_str = f"{score:.3f}"

                                node_info = f" **[{node_type.upper()}: {node_name}]**" if node_type and node_name else ""
                                st.write(
                                    f"**[{idx+1}]**{repo_tag} `{display_filename}`{node_info}  "
                                    f"L{start}–L{end}{tag}  ·  {score_icon} **{score_desc}** *({score_str})*"
                                )
                                if puid:
                                    st.caption(f"PUID: `{puid}`")
                                if qualified_name:
                                    st.caption(f"Qualified: `{qualified_name}`")
                                if source_span:
                                    st.caption(f"Span: `{source_span}`")
                                if modifiers:
                                    st.caption(f"Modifiers: `{modifiers}`")
                                st.code(
                                    d.page_content,
                                    language=meta.get("lang", ""),
                                )

                        if "impact_result" in st.session_state and st.session_state.impact_result:
                            impact = st.session_state.impact_result
                            with st.expander(f"🔍 [Debug] Impact Analysis ({impact.get('total_count', 0)} nodes)"):
                                st.json(impact)
                            del st.session_state["impact_result"]

                        if "graph_seed_edges" in st.session_state and st.session_state.graph_seed_edges:
                            edges = st.session_state.graph_seed_edges
                            with st.expander(f"🕸️ [Debug] Graph Edges ({len(edges)})"):
                                st.json(edges)
                            del st.session_state["graph_seed_edges"]

                    except Exception as e:
                        st.error(f"❌ Lỗi LLM: {e}")
