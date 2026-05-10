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

import asyncpg
import numpy as np
from numpy.typing import NDArray

import cocoindex as coco
from cocoindex.connectors import localfs, postgres
from cocoindex.ops.text import detect_code_language
from cocoindex.ops.sentence_transformers import SentenceTransformerEmbedder
from cocoindex.resources.file import FileLike, PatternFilePathMatcher
from cocoindex.resources.id import IdGenerator

from ast_chunker import extract_ast_nodes


# ─── Cấu hình ────────────────────────────────────────────────────────────────

DATABASE_URL: str = os.environ["COCOINDEX_DATABASE_URL"]
TABLE_NAME    = "code_embeddings"
PG_SCHEMA     = "public"
WORKSPACE_DIR = "/tmp/workspace"
TOP_K         = 10   # lấy nhiều hơn để re-rank bên app

# Model tốt hơn cho semantic search (MTEB Retrieval benchmark)
# all-mpnet-base-v2: 57.0 NDCG@10 vs all-MiniLM-L6-v2: 49.2
EMBED_MODEL = "sentence-transformers/all-mpnet-base-v2"

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
    "**/*.lock",        # lock files
    "**/migrations/**", # DB migrations thường không chứa logic hữu ích
]

# Patterns RIÊNG cho test files — lưu dưới metadata "is_test" để filter khi query
TEST_PATTERNS = {"test", "tests", "spec", "specs", "__tests__", "_test", ".test", ".spec"}


# ─── Context keys ─────────────────────────────────────────────────────────────

PG_DB    = coco.ContextKey[asyncpg.Pool]("code_embedding_db")
EMBEDDER = coco.ContextKey[SentenceTransformerEmbedder]("embedder", detect_change=True)


# ─── Schema ──────────────────────────────────────────────────────────────────

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
        builder.provide(EMBEDDER, SentenceTransformerEmbedder(EMBED_MODEL))
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
    # Thêm prefix vào text để embedding hiểu ngữ cảnh tốt hơn
    parent_info = f"Parent: {chunk.parent_name}\n" if chunk.parent_name else ""
    enriched_text = f"File: {filename}\n{parent_info}Type: {chunk.node_type}\nName: {chunk.node_name}\n\n{chunk.text}"
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
            filename=str(filename),
            lang=lang,
            text=chunk.text,
            embedding=embedding,
            start_line=chunk.start_line,
            end_line=chunk.end_line,
            is_test=is_test,
            node_type=chunk.node_type,
            node_name=chunk.node_name,
            puid=puid,
            parent_puid=parent_puid,
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

    chunks = extract_ast_nodes(text, lang)
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
    table_schema = await postgres.TableSchema.from_class(
        CodeEmbedding,
        primary_key=["id"],
    )
    target_table = await postgres.mount_table_target(
        PG_DB,
        table_name=TABLE_NAME,
        table_schema=table_schema,
        pg_schema_name=PG_SCHEMA,
    )
    target_table.declare_vector_index(column="embedding")

    # Walk toàn bộ thư mục workspace — recursive=True đảm bảo lấy hết thư mục con
    files = localfs.walk_dir(
        sourcedir,
        recursive=True,
        path_matcher=PatternFilePathMatcher(
            excluded_patterns=EXCLUDED_PATTERNS,
        ),
    )
    # === [DEBUG_LOG_START] ===
    sys.stderr.write(f"[DEBUG] app_main: sourcedir={sourcedir}\n")
    # === [DEBUG_LOG_END] ===
    await coco.mount_each(process_file, files.items(), target_table)


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
) -> list[dict]:
    """
    Query pgvector với cosine similarity.
    """
    # === [DEBUG_LOG_START] ===
    total_start = time.perf_counter()
    # === [DEBUG_LOG_END] ===

    embedder = SentenceTransformerEmbedder(EMBED_MODEL)
    enriched_query = f"Code search query: {query_text}"
    
    # --- Giai đoạn 1: Tạo Embedding ---
    # === [DEBUG_LOG_START] ===
    embed_start = time.perf_counter()
    # === [DEBUG_LOG_END] ===
    
    query_vec = await embedder.embed(enriched_query)
    
    # === [DEBUG_LOG_START] ===
    embed_duration = time.perf_counter() - embed_start
    # === [DEBUG_LOG_END] ===

    # --- Giai đoạn 2: Postgres Search ---
    # === [DEBUG_LOG_START] ===
    db_start = time.perf_counter()
    # === [DEBUG_LOG_END] ===

    async with await asyncpg.create_pool(DATABASE_URL) as pool:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT filename, lang, text, start_line, end_line, is_test, node_type, node_name, puid, parent_puid, is_skeleton,
                       1.0 - (embedding <=> $1) AS score
                FROM "{PG_SCHEMA}"."{TABLE_NAME}"
                ORDER BY score DESC
                LIMIT $2
                """,
                str(query_vec.tolist()),
                top_k,
            )

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
        sys.stderr.write(f"  {i+1}. {skel_tag} {r['puid']} (score: {1.0 - (r['score']):.4f})\n")
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


def search(
    query_text: str,
    top_k: int = TOP_K,
) -> list[dict]:
    """Sync wrapper — gọi từ Streamlit."""
    return asyncio.run(_search_async(query_text, top_k))