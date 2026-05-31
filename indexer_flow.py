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
from graph_edge_extractor import extract_graph_edges
from graph_node_contract import canonicalize_node_kind


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
    repo_name: str   # Tên repository/thư mục nguồn rút gọn
    qualified_name: str = ""
    signature: str = ""
    docstring: str = ""
    modifiers: str = ""
    export_status: str = "unknown"
    source_span: str = ""


# ─── Helper: phát hiện test file & trích xuất repo_name ───────────────────────

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


def extract_repo_name(filepath: str) -> str:
    """Trích xuất tên repository từ đường dẫn lưu trong workspace."""
    normalized = filepath.replace("\\", "/")
    prefix = "/tmp/workspace/"
    if normalized.startswith(prefix):
        relative = normalized[len(prefix):]
    else:
        parts = normalized.split("/")
        if "workspace" in parts:
            idx = parts.index("workspace")
            relative = "/".join(parts[idx+1:])
            if not relative:
                relative = normalized
        else:
            relative = normalized
    
    subdir = relative.split("/")[0]
    parts = subdir.rsplit('_', 1)
    if len(parts) == 2 and len(parts[1]) == 6:
        return parts[0]
    return subdir


def extract_workspace_relative_path(filepath: str) -> str:
    """Return the path inside the repo/workspace, excluding the repo folder prefix."""
    normalized = filepath.replace("\\", "/")
    prefix = "/tmp/workspace/"
    if normalized.startswith(prefix):
        relative = normalized[len(prefix):]
    else:
        parts = normalized.split("/")
        if "workspace" in parts:
            idx = parts.index("workspace")
            relative = "/".join(parts[idx + 1 :])
        else:
            relative = normalized

    parts = relative.split("/", 1)
    if len(parts) == 2 and "_" in parts[0]:
        return parts[1]
    return relative


def normalize_puid(
    repo_name: str,
    relative_path: str,
    kind: str,
    qualified_name: str,
) -> str:
    """Build a stable graph identifier for a node."""
    repo = sanitize_for_pg(repo_name).strip() or "unknown_repo"
    rel_path = sanitize_for_pg(relative_path).replace("\\", "/").strip().strip("/")
    node_kind = sanitize_for_pg(kind).strip() or "node"
    qname = sanitize_for_pg(qualified_name).replace("\\", "/").strip()
    return f"{repo}::{rel_path}::{node_kind}::{qname}"


def _build_file_chunk(filepath: pathlib.PurePath, text: str, lang: str):
    """Create a synthetic file-level chunk for every source file."""
    from ast_chunker import AstChunk

    relative_path = extract_workspace_relative_path(str(filepath))
    line_count = max(1, text.count("\n") + 1)
    preview = text[:1600].rstrip()
    node_name = filepath.name or "global"
    return AstChunk(
        text=(
            f"File: {relative_path}\n"
            f"Language: {lang or 'unknown'}\n"
            f"QualifiedName: {relative_path}\n"
            f"SourceSpan: L1-L{line_count}\n\n"
            f"{preview}"
        ),
        node_type="file",
        node_name=node_name,
        qualified_name=relative_path,
        signature=relative_path,
        docstring="",
        modifiers="",
        export_status="internal",
        start_line=1,
        end_line=line_count,
        source_span=f"L1-L{line_count}",
    )


def get_graph_edge_table_name(table_name: str) -> str:
    return f"{table_name}_graph_edges"


async def persist_graph_edges(edges: list, table_name: str) -> None:
    if not edges:
        return

    rows = [
        (
            edge.id,
            sanitize_for_pg(edge.repo_name),
            sanitize_for_pg(edge.filename),
            sanitize_for_pg(edge.lang),
            sanitize_for_pg(edge.edge_type),
            sanitize_for_pg(edge.resolution_status),
            float(edge.confidence),
            sanitize_for_pg(edge.source_puid),
            sanitize_for_pg(edge.target_puid),
            sanitize_for_pg(edge.source_symbol),
            sanitize_for_pg(edge.target_symbol),
            int(edge.source_line or 0),
            int(edge.target_line or 0),
            sanitize_for_pg(edge.metadata),
        )
        for edge in edges
    ]

    create_sql = f"""
        CREATE TABLE IF NOT EXISTS "{PG_SCHEMA}"."{table_name}" (
            id TEXT PRIMARY KEY,
            repo_name VARCHAR,
            filename VARCHAR,
            lang VARCHAR,
            edge_type VARCHAR,
            resolution_status VARCHAR,
            confidence DOUBLE PRECISION,
            source_puid VARCHAR,
            target_puid VARCHAR,
            source_symbol TEXT,
            target_symbol TEXT,
            source_line INT,
            target_line INT,
            metadata TEXT
        )
    """
    index_sql = [
        f'CREATE INDEX IF NOT EXISTS idx_{table_name}_source_puid ON "{PG_SCHEMA}"."{table_name}" (source_puid)',
        f'CREATE INDEX IF NOT EXISTS idx_{table_name}_target_puid ON "{PG_SCHEMA}"."{table_name}" (target_puid)',
        f'CREATE INDEX IF NOT EXISTS idx_{table_name}_edge_type ON "{PG_SCHEMA}"."{table_name}" (edge_type)',
        f'CREATE INDEX IF NOT EXISTS idx_{table_name}_repo_name ON "{PG_SCHEMA}"."{table_name}" (repo_name)',
    ]

    insert_sql = f"""
        INSERT INTO "{PG_SCHEMA}"."{table_name}" (
            id, repo_name, filename, lang, edge_type, resolution_status, confidence,
            source_puid, target_puid, source_symbol, target_symbol, source_line, target_line, metadata
        )
        VALUES (
            $1, $2, $3, $4, $5, $6, $7,
            $8, $9, $10, $11, $12, $13, $14
        )
        ON CONFLICT (id) DO UPDATE SET
            repo_name = EXCLUDED.repo_name,
            filename = EXCLUDED.filename,
            lang = EXCLUDED.lang,
            edge_type = EXCLUDED.edge_type,
            resolution_status = EXCLUDED.resolution_status,
            confidence = EXCLUDED.confidence,
            source_puid = EXCLUDED.source_puid,
            target_puid = EXCLUDED.target_puid,
            source_symbol = EXCLUDED.source_symbol,
            target_symbol = EXCLUDED.target_symbol,
            source_line = EXCLUDED.source_line,
            target_line = EXCLUDED.target_line,
            metadata = EXCLUDED.metadata
    """

    async with await asyncpg.create_pool(DATABASE_URL) as pool:
        async with pool.acquire() as conn:
            await conn.execute(create_sql)
            for stmt in index_sql:
                await conn.execute(stmt)
            await conn.executemany(insert_sql, rows)


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
    repo_name: str,
) -> None:
    # Thêm prefix vào text để embedding hiểu ngữ cảnh tốt hơn (sử dụng profile.document_prefix)
    from embedder_config import load_active_profile
    _prof = load_active_profile()
    prefix = _prof.document_prefix or ""
    relative_path = extract_workspace_relative_path(str(filename))
    parent_info = f"Parent: {chunk.parent_name}\n" if chunk.parent_name else ""
    qname = chunk.qualified_name or chunk.node_name
    node_kind = canonicalize_node_kind(
        chunk.node_type,
        is_file_node=chunk.node_type == "file",
        is_skeleton=chunk.is_skeleton,
    )
    meta_info = (
        f"File: {relative_path}\n"
        f"QualifiedName: {qname}\n"
        f"Type: {node_kind}\n"
        f"Name: {chunk.node_name}\n"
    )
    if chunk.signature:
        meta_info += f"Signature: {chunk.signature}\n"
    if chunk.source_span:
        meta_info += f"SourceSpan: {chunk.source_span}\n"
    if chunk.docstring:
        meta_info += f"Docstring: {chunk.docstring}\n"
    if chunk.modifiers:
        meta_info += f"Modifiers: {chunk.modifiers}\n"
    if chunk.export_status:
        meta_info += f"ExportStatus: {chunk.export_status}\n"
    enriched_text = f"{prefix}{meta_info}{parent_info}\n{chunk.text}"
    async with _embed_sem:
        embedding = await coco.use_context(EMBEDDER).embed(enriched_text)

    # Tạo Semantic PUID
    puid = normalize_puid(repo_name, relative_path, node_kind, qname)
    if chunk.parent_qualified_name:
        parent_kind = chunk.parent_node_type or "node"
        parent_puid = normalize_puid(repo_name, relative_path, parent_kind, chunk.parent_qualified_name)
    elif chunk.node_type == "file":
        parent_puid = ""
    else:
        parent_puid = normalize_puid(repo_name, relative_path, "file", relative_path)

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
            node_type=sanitize_for_pg(node_kind),
            node_name=sanitize_for_pg(chunk.node_name),
            puid=sanitize_for_pg(puid),
            parent_puid=sanitize_for_pg(parent_puid),
            is_skeleton=chunk.is_skeleton,
            repo_name=sanitize_for_pg(repo_name),
            qualified_name=sanitize_for_pg(qname),
            signature=sanitize_for_pg(chunk.signature),
            docstring=sanitize_for_pg(chunk.docstring),
            modifiers=sanitize_for_pg(chunk.modifiers),
            export_status=sanitize_for_pg(chunk.export_status),
            source_span=sanitize_for_pg(chunk.source_span or f"L{chunk.start_line}-L{chunk.end_line}"),
        )
    )


@coco.fn(memo=True)
async def process_file(
    file: FileLike,
    table: postgres.TableTarget[CodeEmbedding],
) -> None:
    from embedder_config import load_active_profile
    act_prof = load_active_profile()

    text = await file.read_text()
    filepath = file.file_path.path
    # === [DEBUG_LOG_START] ===
    sys.stderr.write(f"[DEBUG] VÀO process_file: {filepath}\n")
    # === [DEBUG_LOG_END] ===
    lang = detect_code_language(filename=str(filepath.name)) or ""
    is_test = _is_test_file(filepath)

    newline = '\n'
    
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
                    end_line=max(1, c.text.count('\n') + 1),
                    qualified_name=f"{filepath.name}#chunk_{i}",
                    signature=c.text.splitlines()[0].strip() if c.text.splitlines() else "",
                    docstring="",
                    modifiers="",
                    export_status="internal",
                    source_span=f"L1-L{max(1, c.text.count(newline) + 1)}",
                ))

    file_chunk = _build_file_chunk(filepath, text, lang)
    chunks = [file_chunk] + chunks

    repo_name = extract_repo_name(str(filepath))
    id_gen = IdGenerator()
    await coco.map(
        process_chunk,
        chunks,
        filepath,
        lang,
        is_test,
        id_gen,
        table,
        repo_name,
    )

    graph_edges = extract_graph_edges(
        str(filepath),
        text,
        lang,
        chunks,
        repo_name,
        normalize_puid,
    )
    edge_table_name = get_graph_edge_table_name(act_prof.table_name)
    await persist_graph_edges(graph_edges, edge_table_name)


@coco.fn
async def app_main(sourcedir: pathlib.Path, **kwargs) -> None:
    from embedder_config import load_active_profile
    act_prof = load_active_profile()
    
    # MIGRATION: Thêm cột repo_name nếu bảng đã tồn tại nhưng chưa có cột này.
    # Chúng ta chạy trước mount_table_target để tránh lỗi khớp schema.
    async with await asyncpg.create_pool(DATABASE_URL) as pool:
        try:
            table_exists = await pool.fetchval(
                f"SELECT EXISTS (SELECT FROM information_schema.tables WHERE table_schema = '{PG_SCHEMA}' AND table_name = '{act_prof.table_name}')"
            )
            if table_exists:
                for column_sql in [
                    'ADD COLUMN IF NOT EXISTS repo_name VARCHAR',
                    'ADD COLUMN IF NOT EXISTS qualified_name VARCHAR',
                    'ADD COLUMN IF NOT EXISTS signature VARCHAR',
                    'ADD COLUMN IF NOT EXISTS docstring TEXT',
                    'ADD COLUMN IF NOT EXISTS modifiers VARCHAR',
                    'ADD COLUMN IF NOT EXISTS export_status VARCHAR',
                    'ADD COLUMN IF NOT EXISTS source_span VARCHAR',
                ]:
                    await pool.execute(f'ALTER TABLE "{PG_SCHEMA}"."{act_prof.table_name}" {column_sql}')
        except Exception as e:
            sys.stderr.write(f"[MIGRATION WARNING] Failed to add repo_name column: {e}\n")
            sys.stderr.flush()

        edge_table_name = get_graph_edge_table_name(act_prof.table_name)
        try:
            await pool.execute(
                f'''
                CREATE TABLE IF NOT EXISTS "{PG_SCHEMA}"."{edge_table_name}" (
                    id TEXT PRIMARY KEY,
                    repo_name VARCHAR,
                    filename VARCHAR,
                    lang VARCHAR,
                    edge_type VARCHAR,
                    resolution_status VARCHAR,
                    confidence DOUBLE PRECISION,
                    source_puid VARCHAR,
                    target_puid VARCHAR,
                    source_symbol TEXT,
                    target_symbol TEXT,
                    source_line INT,
                    target_line INT,
                    metadata TEXT
                )
                '''
            )
            for stmt in [
                f'CREATE INDEX IF NOT EXISTS idx_{edge_table_name}_source_puid ON "{PG_SCHEMA}"."{edge_table_name}" (source_puid)',
                f'CREATE INDEX IF NOT EXISTS idx_{edge_table_name}_target_puid ON "{PG_SCHEMA}"."{edge_table_name}" (target_puid)',
                f'CREATE INDEX IF NOT EXISTS idx_{edge_table_name}_edge_type ON "{PG_SCHEMA}"."{edge_table_name}" (edge_type)',
                f'CREATE INDEX IF NOT EXISTS idx_{edge_table_name}_repo_name ON "{PG_SCHEMA}"."{edge_table_name}" (repo_name)',
            ]:
                await pool.execute(stmt)
        except Exception as e:
            sys.stderr.write(f"[MIGRATION WARNING] Failed to create edge table: {e}\n")
            sys.stderr.flush()

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
            
    # MIGRATION: Thêm cột text_search và GIN index cho full-text search
    async with await asyncpg.create_pool(DATABASE_URL) as pool:
        try:
            # Xoá cột cũ (nếu có) để đảm bảo nó được tạo lại dưới dạng GENERATED ALWAYS có dữ liệu
            await pool.execute(f'ALTER TABLE "{PG_SCHEMA}"."{act_prof.table_name}" DROP COLUMN IF EXISTS text_search')
            await pool.execute(f'ALTER TABLE "{PG_SCHEMA}"."{act_prof.table_name}" ADD COLUMN text_search tsvector GENERATED ALWAYS AS (to_tsvector(\'english\'::regconfig, COALESCE(node_name, \'\') || \' \' || COALESCE(text, \'\'))) STORED')
        except Exception as e:
            sys.stderr.write(f"[MIGRATION ERROR] Failed to create text_search column: {e}\n")
            
        try:
            await pool.execute(f'CREATE INDEX IF NOT EXISTS idx_text_search ON "{PG_SCHEMA}"."{act_prof.table_name}" USING GIN (text_search)')
        except Exception as e:
            sys.stderr.write(f"[MIGRATION ERROR] Failed to create index: {e}\n")

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

def rrf_merge(vector_results, bm25_results, k: int = 60):
    """Reciprocal Rank Fusion of two result lists.
    Each result dict must contain a unique 'puid' key.
    Returns a list sorted by combined RRF score.
    """
    scores: dict[str, float] = {}
    merged: dict[str, dict] = {}
    for rank, r in enumerate(vector_results):
        puid = r.get('puid')
        if puid:
            scores[puid] = scores.get(puid, 0) + 1 / (k + rank + 1)
            merged[puid] = r
    for rank, r in enumerate(bm25_results):
        puid = r.get('puid')
        if puid:
            scores[puid] = scores.get(puid, 0) + 1 / (k + rank + 1)
            merged[puid] = r
    for puid, r in merged.items():
        r['_rrf_score'] = scores[puid]
        
    sorted_merged = sorted(merged.values(), key=lambda x: x['_rrf_score'], reverse=True)
    
    sys.stderr.write(f"\n[RRF_MERGE] Merged {len(vector_results)} Vector + {len(bm25_results)} BM25 results -> {len(sorted_merged)} unique.\n")
    for i, r in enumerate(sorted_merged[:15]): # Chỉ log top 15 để đỡ rối
        sys.stderr.write(f"  {i+1}. [RRF] {r['puid']} (rrf_score: {r['_rrf_score']:.4f})\n")
    sys.stderr.write("=============================================\n")
    sys.stderr.flush()

    # Sort by the RRF score descending
    return sorted_merged


def _row_to_result(r) -> dict:
    """Normalize a database row into the dict shape used by search callers."""
    return {
        "filename":    r["filename"],
        "lang":        r["lang"],
        "text":        r["text"],
        "score":       float(r["score"]),
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
        "puid":        r["puid"],
        "parent_puid": r.get("parent_puid", ""),
        "is_skeleton": r.get("is_skeleton", False),
        "repo_name":   r.get("repo_name", ""),
    }

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

    # === [DEBUG_LOG_START] ===
    embed_duration = time.perf_counter() - embed_start
    # === [DEBUG_LOG_END] ===

    # --- Giai đoạn 2: Postgres Search ---
    # === [DEBUG_LOG_START] ===
    db_start = time.perf_counter()
    # === [DEBUG_LOG_END] ===

    # Xây dựng SQL filter nếu có source_filters
    filter_sql = ""
    final_source_filters = []
    if source_filters:
        for f in source_filters:
            dst_name = get_workspace_subdir(f)
            parts = dst_name.rsplit('_', 1)
            if len(parts) == 2 and len(parts[1]) == 6:
                final_source_filters.append(parts[0])
            else:
                final_source_filters.append(dst_name)
        filter_sql = "AND repo_name = ANY($3)"

    from pgvector.asyncpg import register_vector
    async def _init(conn):
        await register_vector(conn)

    async with await asyncpg.create_pool(DATABASE_URL, init=_init) as pool:
        async with pool.acquire() as conn:
            query = f"""
                SELECT filename, lang, text, start_line, end_line, is_test, node_type, node_name, qualified_name, signature, docstring, modifiers, export_status, source_span, puid, parent_puid, is_skeleton, repo_name,
                       1.0 - (embedding <=> $1) AS score
                FROM "{PG_SCHEMA}"."{act_prof.table_name}"
                WHERE 1=1 {filter_sql}
                ORDER BY score DESC
                LIMIT $2
            """
            params = [query_vec.tolist(), top_k]
            if final_source_filters:
                params.append(final_source_filters)
            
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
    return [_row_to_result(r) for r in rows]


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
        SELECT filename, lang, text, start_line, end_line, is_test, node_type, node_name, qualified_name, signature, docstring, modifiers, export_status, source_span, puid, parent_puid, is_skeleton, repo_name,
               1.0 AS score
        FROM "{PG_SCHEMA}"."{act_prof.table_name}"
        WHERE puid = ANY($1)
    """
    if is_skeleton is not None:
        query += f" AND is_skeleton = {is_skeleton}"

    async with await asyncpg.create_pool(DATABASE_URL) as pool:
        async with pool.acquire() as conn:
            rows = await conn.fetch(query, puids)

    return [_row_to_result(r) for r in rows]


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


async def fetch_edges_by_puid_async(
    puids: List[str],
    direction: str = "both",
) -> list[dict]:
    """Fetch graph edges touching the given PUIDs."""
    if not puids:
        return []

    from embedder_config import load_active_profile
    act_prof = load_active_profile()
    edge_table_name = get_graph_edge_table_name(act_prof.table_name)

    direction = (direction or "both").lower()
    if direction == "incoming":
        clause = "target_puid = ANY($1)"
    elif direction == "outgoing":
        clause = "source_puid = ANY($1)"
    else:
        clause = "(source_puid = ANY($1) OR target_puid = ANY($1))"

    query = f"""
        SELECT id, repo_name, filename, lang, edge_type, resolution_status, confidence,
               source_puid, target_puid, source_symbol, target_symbol, source_line, target_line, metadata
        FROM "{PG_SCHEMA}"."{edge_table_name}"
        WHERE {clause}
        ORDER BY edge_type, source_puid, target_puid, source_line
    """

    async with await asyncpg.create_pool(DATABASE_URL) as pool:
        async with pool.acquire() as conn:
            rows = await conn.fetch(query, puids)

    return [dict(r) for r in rows]


def fetch_edges_by_puid(
    puids: List[str],
    direction: str = "both",
) -> list[dict]:
    """Sync wrapper for edge lookups."""
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    if loop.is_closed():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(fetch_edges_by_puid_async(puids, direction))


def search(
    query_text: str,
    top_k: int = TOP_K,
    source_filters: Optional[List[str]] = None,
) -> list[dict]:
    """Sync wrapper — quản lý loop an toàn cho Streamlit."""
    return asyncio.run(_search_async(query_text, top_k, source_filters))


async def _fulltext_search_async(
    query_text: str,
    top_k: int = TOP_K,
    source_filters: Optional[List[str]] = None,
) -> list[dict]:
    """Full‑text BM25‑like search using PostgreSQL tsvector."""
    from embedder_config import load_active_profile
    act_prof = load_active_profile()

    filter_sql = ""
    params: list = []
    
    # The query text is parameter $1
    params.append(query_text)
    
    if source_filters:
        final_source_filters = []
        for f in source_filters:
            dst_name = get_workspace_subdir(f)
            parts = dst_name.rsplit('_', 1)
            if len(parts) == 2 and len(parts[1]) == 6:
                final_source_filters.append(parts[0])
            else:
                final_source_filters.append(dst_name)
        params.append(final_source_filters)
        filter_sql = "AND repo_name = ANY($2)"

    param_idx_topk = len(params) + 1
    params.append(top_k)

    param_idx_query = 1

    sql = f"""
        WITH query_ts AS (
            SELECT replace(plainto_tsquery('english', ${param_idx_query})::text, '&', '|')::tsquery AS q
        )
        SELECT filename, lang, text, start_line, end_line, is_test, node_type, node_name, qualified_name, signature, docstring, modifiers, export_status, source_span, puid, parent_puid, is_skeleton, repo_name,
               ts_rank_cd(text_search, query_ts.q) AS score
        FROM "{PG_SCHEMA}"."{act_prof.table_name}", query_ts
        WHERE text_search @@ query_ts.q
        {filter_sql}
        ORDER BY score DESC
        LIMIT ${param_idx_topk}
    """
    
    bm25_start = time.perf_counter()

    async with await asyncpg.create_pool(DATABASE_URL) as pool:
        async with pool.acquire() as conn:
            rows = await conn.fetch(sql, *params)
            
    bm25_duration = time.perf_counter() - bm25_start
    
    sys.stderr.write(f"\n[BM25_SEARCH] Query: '{query_text}'\n")
    sys.stderr.write(f"[BM25_SEARCH] Time: {bm25_duration:.4f}s\n")
    sys.stderr.write(f"[BM25_SEARCH] Found {len(rows)} nodes:\n")
    for i, r in enumerate(rows):
        sys.stderr.write(f"  {i+1}. [BM25] {r['puid']} (rank_score: {r['score']:.4f})\n")
    sys.stderr.write("=============================================\n")
    sys.stderr.flush()

    return [_row_to_result(r) for r in rows]


def fulltext_search(
    query_text: str,
    top_k: int = TOP_K,
    source_filters: Optional[List[str]] = None,
) -> list[dict]:
    """Sync wrapper cho fulltext search."""
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    
    if loop.is_closed():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
    return loop.run_until_complete(_fulltext_search_async(query_text, top_k, source_filters))


# ─────────────────────────────────────────────────────────────────────────────
# Task 3.5.1 – Per‑repo helpers for multi‑repo diversity
# ─────────────────────────────────────────────────────────────────────────────

async def _get_all_repo_names_async() -> list[str]:
    """Return distinct repo_name values present in the index table."""
    from embedder_config import load_active_profile
    act_prof = load_active_profile()
    async with await asyncpg.create_pool(DATABASE_URL) as pool:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT DISTINCT repo_name"
                f" FROM \"{PG_SCHEMA}\".\"{act_prof.table_name}\""
                f" WHERE repo_name IS NOT NULL AND repo_name <> ''"
            )
    return [r["repo_name"] for r in rows]


def get_all_repo_names() -> list[str]:
    """Sync wrapper – returns distinct repo names from the index."""
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    if loop.is_closed():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(_get_all_repo_names_async())


# ── per‑repo vector search ────────────────────────────────────────────────────

async def _search_per_repo_async(
    query_text: str,
    top_k: int = TOP_K,
    repo_name: str = "",
) -> list[dict]:
    """Vector (cosine) search restricted to a single repository."""
    from embedder_config import load_active_profile
    act_prof = load_active_profile()
    model = get_query_model()
    # Encode query to vector (list of floats)
    query_vec = model.encode([query_text], normalize_embeddings=True)[0]
    # Register pgvector type on each connection in the pool
    from pgvector.asyncpg import register_vector
    async def _init(conn):
        await register_vector(conn)

    async with await asyncpg.create_pool(DATABASE_URL, init=_init) as pool:
        async with pool.acquire() as conn:
            sql = f"""
                SELECT filename, lang, text, start_line, end_line,
                       is_test, node_type, node_name, qualified_name, signature, docstring, modifiers, export_status, source_span, puid, parent_puid,
                       is_skeleton, repo_name,
                       1.0 - (embedding <=> $1) AS score
                FROM "{PG_SCHEMA}"."{act_prof.table_name}"
                WHERE repo_name = $2
                ORDER BY score DESC
                LIMIT $3
            """
            rows = await conn.fetch(sql, query_vec.tolist(), repo_name, top_k)

    return [_row_to_result(r) for r in rows]


def search_per_repo(
    query_text: str,
    top_k: int = TOP_K,
    repo_name: str = "",
) -> list[dict]:
    """Sync wrapper for per‑repo vector search."""
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    if loop.is_closed():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(_search_per_repo_async(query_text, top_k, repo_name))


# ── per‑repo full‑text (BM25) search ─────────────────────────────────────────

async def _fulltext_search_per_repo_async(
    query_text: str,
    top_k: int = TOP_K,
    repo_name: str = "",
) -> list[dict]:
    """Full‑text (BM25/ts_rank) search restricted to a single repository."""
    from embedder_config import load_active_profile
    act_prof = load_active_profile()

    sql = f"""
        WITH query_ts AS (
            SELECT replace(plainto_tsquery('english', $1)::text, '&', '|')::tsquery AS q
        )
        SELECT filename, lang, text, start_line, end_line,
               is_test, node_type, node_name, qualified_name, signature, docstring, modifiers, export_status, source_span, puid, parent_puid,
               is_skeleton, repo_name,
               ts_rank_cd(text_search, query_ts.q) AS score
        FROM "{PG_SCHEMA}"."{act_prof.table_name}", query_ts
        WHERE text_search @@ query_ts.q
          AND repo_name = $2
        ORDER BY score DESC
        LIMIT $3
    """
    async with await asyncpg.create_pool(DATABASE_URL) as pool:
        async with pool.acquire() as conn:
            rows = await conn.fetch(sql, query_text, repo_name, top_k)

    return [_row_to_result(r) for r in rows]


def fulltext_search_per_repo(
    query_text: str,
    top_k: int = TOP_K,
    repo_name: str = "",
) -> list[dict]:
    """Sync wrapper for per‑repo full‑text search."""
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    if loop.is_closed():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(_fulltext_search_per_repo_async(query_text, top_k, repo_name))
