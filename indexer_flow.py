"""
CocoIndex v1.x — Source Code Indexing Pipeline
===============================================

Cải tiến so với phiên bản cũ:
1. Model: thay all-MiniLM-L6-v2 → "sentence-transformers/all-mpnet-base-v2"
   - Cao hơn 5-10% NDCG trên benchmark retrieval so với MiniLM-L6
   - Vẫn run được CPU (384-dim vs 768-dim, đánh đổi nhỏ về tốc độ)
   - Nếu muốn chuyên code hơn, thay bằng "flax-sentence-embeddings/st-codesearch-distilroberta-base"

2. Chunking: lưu thêm start_line/end_line + prefix filename vào text chunk
   → embedding mang thêm ngữ cảnh "file nào, dòng nào" → giảm nhầm lẫn giữa test vs logic

3. Schema: thêm start_line, end_line, chunk_index → query có thể filter/boost

4. Walk: dùng localfs.walk_dir với recursive=True + excluded_patterns chuẩn
   → tránh index test files bằng cách tách excluded_patterns và lưu file_type metadata

5. Search: query với pgvector cosine + ORDER BY score DESC + LIMIT
   → thêm optional filter theo file_type để loại test files khi user muốn
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import sys
import time
from dataclasses import dataclass
from typing import Annotated, AsyncIterator

MAX_EMBED_CONCURRENCY = int(os.getenv("MAX_EMBED_CONCURRENCY", "8"))
_embed_sem = asyncio.Semaphore(MAX_EMBED_CONCURRENCY)

import asyncpg
import numpy as np

from numpy.typing import NDArray

import cocoindex as coco
from cocoindex.connectors import localfs, postgres
from cocoindex.ops.text import detect_code_language, RecursiveSplitter
from cocoindex.ops.sentence_transformers import SentenceTransformerEmbedder
from cocoindex.resources.file import FileLike, PatternFilePathMatcher
from cocoindex.resources.id import IdGenerator
from sentence_transformers import SentenceTransformer

from ast_chunker import extract_ast_nodes


# ─── Cấu hình ────────────────────────────────────────────────────────────────

DATABASE_URL: str = os.environ["COCOINDEX_DATABASE_URL"]
PG_SCHEMA     = "public"
WORKSPACE_DIR = "/tmp/workspace"

# GLOBAL SINGLETON
_QUERY_MODEL = None
_CURRENT_MODEL_ID = None

def get_query_model():
    global _QUERY_MODEL
    global _CURRENT_MODEL_ID

    from embedder_config import load_active_profile
    prof = load_active_profile()
    
    if _QUERY_MODEL is None or _CURRENT_MODEL_ID != prof.model_id:
        

        sys.stderr.write(
            f"[SEARCH] Loading model: {prof.model_id}\n"
        )
        sys.stderr.flush()

        _QUERY_MODEL = SentenceTransformer(
            prof.model_id,
            trust_remote_code=prof.trust_remote_code,
        )

        # nomic optimization
        _QUERY_MODEL.tokenizer.model_max_length = 256

        # warmup
        _QUERY_MODEL.encode(
            ["search_query: warmup"],
            normalize_embeddings=True,
        )
        
        _CURRENT_MODEL_ID = prof.model_id

        sys.stderr.write("[SEARCH] Warmup complete\n")
        sys.stderr.flush()

    return _QUERY_MODEL

from embedder_config import load_active_profile
_active_profile = load_active_profile()
TABLE_NAME    = _active_profile.table_name
EMBED_MODEL   = _active_profile.model_id

def get_workspace_subdir(original_src: str) -> str:
    """Tạo tên thư mục đích duy nhất trong workspace dựa trên hash đường dẫn chuẩn hóa."""
    import hashlib
    # Chuẩn hóa: chữ thường, gạch chéo xuôi, bỏ gạch chéo cuối
    normalized = original_src.replace("\\", "/").lower().rstrip("/")
    path_hash = hashlib.md5(normalized.encode()).hexdigest()[:6]
    folder_name = os.path.basename(normalized)
    return f"{folder_name}_{path_hash}"
TOP_K         = 10   # lấy nhiều hơn để re-rank bên app

# Patterns thư mục/file cần loại khỏi index
EXCLUDED_PATTERNS = [
    "**/.*",            # hidden files/folders
    "**/node_modules",
    "**/bin", "**/obj",
    "**/__pycache__",
    "**/*.pyc",
    "**/dist", "**/build",
    "**/.vs", "**/.vscode", "**/.idea",
    "**/*.min.js",      # minified JS không hữu ích
    "**/*.min.css",     # minified CSS
    "**/*.generated.ts",# code generated
    "**/*.d.ts",        # typescript definitions (thường quá lớn và không chứa logic)
    "**/*.lock",        # lock files
    "**/migrations/**", # DB migrations thường không chứa logic hữu ích
    "**/*.png", "**/*.jpg", "**/*.jpeg", "**/*.gif", "**/*.ico", # images
    "**/*.woff", "**/*.woff2", "**/*.ttf", "**/*.eot",          # fonts
    "**/*.pdf", "**/*.zip", "**/*.tar.gz", "**/*.rar",          # archives
]

# Patterns RIÊNG cho test files — lưu dưới metadata "is_test" để filter khi query
TEST_PATTERNS = {"test", "tests", "spec", "specs", "__tests__", "_test", ".test", ".spec"}

# Ngôn ngữ hỗ trợ AST Parser (Tầng 1)
SUPPORTED_AST_LANGS = {"python", "csharp", "c_sharp", "javascript", "js", "typescript", "ts", "tsx"}


# ─── Context keys ─────────────────────────────────────────────────────────────

PG_DB    = coco.ContextKey[asyncpg.Pool]("code_embedding_db")
EMBEDDER = coco.ContextKey[SentenceTransformerEmbedder]("embedder", detect_change=True)


# ─── Schema ──────────────────────────────────────────────────────────────────

def sanitize_for_pg(text: Optional[str]) -> str:
    """Loại bỏ ký tự null byte (\x00) gây lỗi Postgres."""
    if not text:
        return ""
    return text.replace("\x00", "")

@dataclass
class CodeEmbedding:
    id: int
    filename: str
    lang: str
    text: str
    embedding: Annotated[NDArray, EMBEDDER]
    start_line: int
    end_line: int
    is_test: bool    # True nếu file nằm trong thư mục test hoặc tên có _test/_spec
    node_type: str   # class, function, file, skeleton, ...
    node_name: str   # Tên class/hàm
    puid: str        # Project Unique ID (định danh ngữ nghĩa: file::class::node)
    parent_puid: str # PUID của node cha (nếu có)
    is_skeleton: bool # True nếu node chỉ chứa chữ ký (signatures) cho RAG reasoning


# ─── Helper: phát hiện test file ─────────────────────────────────────────────

def _is_test_file(filepath: pathlib.PurePath) -> bool:
    """Kiểm tra xem file có phải test file không dựa trên tên file và đường dẫn."""
    parts_lower = {p.lower() for p in filepath.parts}
    stem_lower = filepath.stem.lower()
    # Kiểm tra thư mục cha
    if parts_lower & TEST_PATTERNS:
        return True
    # Kiểm tra tên file (test_xxx.py, xxx_test.py, xxx.spec.ts, ...)
    if any(stem_lower.startswith(p) or stem_lower.endswith(p) or f".{p}" in stem_lower
           for p in TEST_PATTERNS):
        return True
    return False


# ─── Lifespan ─────────────────────────────────────────────────────────────────

@coco.lifespan
async def coco_lifespan(builder: coco.EnvironmentBuilder) -> AsyncIterator[None]:
    async with await asyncpg.create_pool(DATABASE_URL) as pool:
        builder.provide(PG_DB, pool)
        from embedder_config import load_active_profile
        act_prof = load_active_profile()
        sys.stderr.write(f"[LIFESPAN] Đang khởi tạo Embedder với model: {act_prof.model_id}\n")
        sys.stderr.flush()
        # Pass trust_remote_code flag if the model requires it
        builder.provide(EMBEDDER, SentenceTransformerEmbedder(act_prof.model_id, trust_remote_code=act_prof.trust_remote_code))
        yield


# ─── Processing functions ─────────────────────────────────────────────────────

@coco.fn
async def process_chunk(
    chunk,
    filename: pathlib.PurePath,
    lang: str,
    is_test: bool,
    id_gen: IdGenerator,
    table: postgres.TableTarget[CodeEmbedding],
) -> None:
    # Thêm prefix vào text để embedding hiểu ngữ cảnh tốt hơn (sử dụng profile.document_prefix)
    from embedder_config import load_active_profile
    _prof = load_active_profile()
    prefix = _prof.document_prefix or ""
    parent_info = f"Parent: {chunk.parent_name}\n" if chunk.parent_name else ""
    enriched_text = f"{prefix}File: {filename}\n{parent_info}Type: {chunk.node_type}\nName: {chunk.node_name}\n\n{chunk.text}"
    async with _embed_sem:
        embedding = await coco.use_context(EMBEDDER).embed(enriched_text)

    # Tạo Semantic PUID
    puid = f"{filename}::{chunk.node_name}"
    parent_puid = f"{filename}::{chunk.parent_name}" if chunk.parent_name else ""

    # === [DEBUG_LOG_START] ===
    skeleton_tag = "[SKELETON]" if chunk.is_skeleton else "[CONTENT]"
    sys.stderr.write(f"[INDEX] {skeleton_tag} PUID: {puid} | Parent: {chunk.parent_name or 'None'}\n")
    sys.stderr.flush()
    # === [DEBUG_LOG_END] ===

    table.declare_row(
        row=CodeEmbedding(
            id=await id_gen.next_id(f"{puid}:{chunk.is_skeleton}"), # ID duy nhất theo puid và loại node
            filename=sanitize_for_pg(str(filename)),
            lang=lang,
            text=sanitize_for_pg(chunk.text),
            embedding=embedding,
            start_line=chunk.start_line,
            end_line=chunk.end_line,
            is_test=is_test,
            node_type=sanitize_for_pg(chunk.node_type),
            node_name=sanitize_for_pg(chunk.node_name),
            puid=sanitize_for_pg(puid),
            parent_puid=sanitize_for_pg(parent_puid),
            is_skeleton=chunk.is_skeleton
        )
    )


@coco.fn(memo=True)
async def process_file(
    file: FileLike,
    table: postgres.TableTarget[CodeEmbedding],
) -> None:
    text = await file.read_text()
    filepath = file.file_path.path
    # === [DEBUG_LOG_START] ===
    sys.stderr.write(f"[DEBUG] VÀO process_file: {filepath}\n")
    # === [DEBUG_LOG_END] ===
    lang = detect_code_language(filename=str(filepath.name)) or ""
    is_test = _is_test_file(filepath)

    if lang in SUPPORTED_AST_LANGS:
        chunks = extract_ast_nodes(text, lang)
    else:
        from ast_chunker import AstChunk
        splitter = RecursiveSplitter()
        # RecursiveSplitter trong CocoIndex v1.x: tham số truyền vào hàm split()
        text_chunks = splitter.split(text, chunk_size=800, chunk_overlap=150)
        chunks = []
        for i, c in enumerate(text_chunks):
            chunks.append(AstChunk(
                text=c.text,
                node_type="file_chunk",
                node_name=f"chunk_{i}",
                start_line=1, # Tạm thời để 1
                end_line=max(1, c.text.count('\n') + 1)
            ))

    id_gen = IdGenerator()
    await coco.map(
        process_chunk,
        chunks,
        filepath,
        lang,
        is_test,
        id_gen,
        table,
    )


@coco.fn
async def app_main(sourcedir: pathlib.Path, **kwargs) -> None:
    from embedder_config import load_active_profile
    act_prof = load_active_profile()
    
    table_schema = await postgres.TableSchema.from_class(
        CodeEmbedding,
        primary_key=["id"],
    )
    try:
        target_table = await postgres.mount_table_target(
            PG_DB,
            table_name=act_prof.table_name,
            table_schema=table_schema,
            pg_schema_name=PG_SCHEMA,
        )
        # Chỉ tạo index nếu bảng mới được tạo hoặc chưa có index
        try:
            target_table.declare_vector_index(column="embedding")
        except Exception:
            # Bỏ qua nếu index đã tồn tại
            pass
    except Exception as e:
        if "already exists" in str(e):
            # Nếu mount thất bại vì bảng đã có, ta thử lấy lại target mà không tạo mới
            target_table = await postgres.mount_table_target(
                PG_DB,
                table_name=act_prof.table_name,
                table_schema=table_schema,
                pg_schema_name=PG_SCHEMA,
            )
        else:
            raise e

    # Walk toàn bộ thư mục workspace — recursive=True đảm bảo lấy hết thư mục con
    files = localfs.walk_dir(
        sourcedir,
        recursive=True,
        path_matcher=PatternFilePathMatcher(
            excluded_patterns=EXCLUDED_PATTERNS,
        ),
    )
    # === [DEBUG_LOG_START] ===
    # Chuyển đổi async generator sang list một cách an toàn
    file_list = [item async for item in files.items()]
    sys.stderr.write(f"[DEBUG] app_main: sourcedir={sourcedir}\n")
    sys.stderr.write(f"[DEBUG] app_main: Found {len(file_list)} files to process.\n")
    for i, (path, _) in enumerate(file_list[:10]): # Log 10 file đầu tiên
        sys.stderr.write(f"  - {i+1}. {path}\n")
    sys.stderr.flush()
    # === [DEBUG_LOG_END] ===
    await coco.mount_each(process_file, file_list, target_table)


# ─── App entry point ──────────────────────────────────────────────────────────

app = coco.App(
    coco.AppConfig(name="CodeEmbedding"),
    app_main,
    sourcedir=pathlib.Path(WORKSPACE_DIR),
)


# ─── Search ───────────────────────────────────────────────────────────────────

async def _search_async(
    query_text: str,
    top_k: int = TOP_K,
    source_filters: Optional[List[str]] = None,
) -> list[dict]:
    """
    Query pgvector với cosine similarity.
    """
    # === [DEBUG_LOG_START] ===
    total_start = time.perf_counter()
    # === [DEBUG_LOG_END] ===

    from embedder_config import load_active_profile
    act_prof = load_active_profile()

    ## embedder = SentenceTransformerEmbedder(act_prof.model_id)
    # Load profile để lấy query_prefix (có thể rỗng)
    from embedder_config import load_active_profile
    _prof = load_active_profile()
    query_prefix = _prof.query_prefix or ""
    enriched_query = f"{query_prefix}{query_text}"
    
    # --- Giai đoạn 1: Tạo Embedding ---
    # === [DEBUG_LOG_START] ===
    embed_start = time.perf_counter()
    # === [DEBUG_LOG_END] ===
    
    model = get_query_model()
    sys.stderr.write(f"[DEBUG] app_main: embedder model={model}\n")
    query_vec = model.encode(
        [enriched_query],
        normalize_embeddings=True,
    )[0]

    sys.stderr.write(f"[DEBUG] app_main: query_vec={query_vec}\n")
    # === [DEBUG_LOG_START] ===
    embed_duration = time.perf_counter() - embed_start
    # === [DEBUG_LOG_END] ===

    # --- Giai đoạn 2: Postgres Search ---
    # === [DEBUG_LOG_START] ===
    db_start = time.perf_counter()
    # === [DEBUG_LOG_END] ===

    # Xây dựng SQL filter nếu có source_filters
    filter_sql = ""
    if source_filters:
        # Chuyển đổi các nguồn được chọn sang đúng tên thư mục trong workspace (/tmp/workspace/Project_Hash)
        mapped_filters = [f"{WORKSPACE_DIR}/{get_workspace_subdir(f)}" for f in source_filters]
        
        conditions = []
        for i, src in enumerate(mapped_filters):
            conditions.append(f"filename LIKE ${i+3} || '%'")
        filter_sql = "AND (" + " OR ".join(conditions) + ")"
        # Cập nhật params cho SQL query
        final_source_filters = mapped_filters
    else:
        final_source_filters = []

    async with await asyncpg.create_pool(DATABASE_URL) as pool:
        async with pool.acquire() as conn:
            query = f"""
                SELECT filename, lang, text, start_line, end_line, is_test, node_type, node_name, puid, parent_puid, is_skeleton,
                       1.0 - (embedding <=> $1) AS score
                FROM "{PG_SCHEMA}"."{act_prof.table_name}"
                WHERE 1=1 {filter_sql}
                ORDER BY score DESC
                LIMIT $2
            """
            params = [str(query_vec.tolist()), top_k]
            if final_source_filters:
                params.extend(final_source_filters)
            
            sys.stderr.write(f"[DEBUG] SQL Filter: {filter_sql}\n")
            sys.stderr.write(f"[DEBUG] Params (source filters): {final_source_filters}\n")
                
            rows = await conn.fetch(query, *params)

    # === [DEBUG_LOG_START] ===
    db_duration = time.perf_counter() - db_start
    total_duration = time.perf_counter() - total_start
    
    sys.stderr.write(f"\n[METRICS] Chi tiết tìm kiếm:\n")
    sys.stderr.write(f"  - Tổng thời gian:      {total_duration:.4f}s\n")
    sys.stderr.write(f"  - Tạo Embedding:       {embed_duration:.4f}s\n")
    sys.stderr.write(f"  - Postgres Search:     {db_duration:.4f}s\n")
    sys.stderr.write(f"\n[SEARCH_RESULTS] Top {len(rows)} nodes:\n")
    for i, r in enumerate(rows):
        skel_tag = "[SKELETON]" if r["is_skeleton"] else "[CONTENT]"
        # r['score'] đã là 1.0 - distance (Similarity)
        sys.stderr.write(f"  {i+1}. {skel_tag} {r['puid']} (similarity: {r['score']:.4f})\n")
    sys.stderr.write(f"=============================================\n")
    sys.stderr.flush()
    # === [DEBUG_LOG_END] ===

    # Chuyển đổi kết quả sang list dict
    results = []
    for r in rows:
        results.append({
            "filename":   r["filename"],
            "lang":       r["lang"],
            "text":       r["text"],
            "score":      float(r["score"]),
            "start_line": r["start_line"],
            "end_line":   r["end_line"],
            "is_test":    r["is_test"],
            "node_type":  r["node_type"],
            "node_name":  r["node_name"],
            "puid":       r["puid"],
            "parent_puid": r["parent_puid"],
            "is_skeleton": r["is_skeleton"],
        })

    return results


async def fetch_nodes_by_puid(
    puids: List[str],
    is_skeleton: Optional[bool] = None
) -> list[dict]:
    """Lấy trực tiếp các node theo danh sách PUID (thường dùng để lấy Skeleton)."""
    if not puids:
        return []
        
    from embedder_config import load_active_profile
    act_prof = load_active_profile()

    query = f"""
        SELECT filename, lang, text, start_line, end_line, is_test, node_type, node_name, puid, parent_puid, is_skeleton,
               1.0 AS score
        FROM "{PG_SCHEMA}"."{act_prof.table_name}"
        WHERE puid = ANY($1)
    """
    if is_skeleton is not None:
        query += f" AND is_skeleton = {is_skeleton}"

    async with await asyncpg.create_pool(DATABASE_URL) as pool:
        async with pool.acquire() as conn:
            rows = await conn.fetch(query, puids)

    results = []
    for r in rows:
        results.append({
            "filename":   r["filename"],
            "lang":       r["lang"],
            "text":       r["text"],
            "score":      float(r["score"]),
            "start_line": r["start_line"],
            "end_line":   r["end_line"],
            "is_test":    r["is_test"],
            "node_type":  r["node_type"],
            "node_name":  r["node_name"],
            "puid":       r["puid"],
            "parent_puid": r["parent_puid"],
            "is_skeleton": r["is_skeleton"],
        })
    return results


def fetch_nodes(
    puids: List[str],
    is_skeleton: Optional[bool] = None
) -> list[dict]:
    """Sync wrapper cho fetch_nodes_by_puid."""
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    
    if loop.is_closed():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
    return loop.run_until_complete(fetch_nodes_by_puid(puids, is_skeleton))


def search(
    query_text: str,
    top_k: int = TOP_K,
    source_filters: Optional[List[str]] = None,
) -> list[dict]:
    """Sync wrapper — quản lý loop an toàn cho Streamlit."""
    return asyncio.run(_search_async(query_text, top_k, source_filters))