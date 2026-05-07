import os
import functools

import numpy as np
from numpy.typing import NDArray
from psycopg_pool import ConnectionPool
from pgvector.psycopg import register_vector

import cocoindex


# --- Bước 1: Định nghĩa transform_flow để embed text (dùng lại cho cả indexing và query-time) ---
@cocoindex.transform_flow()
def code_to_embedding(
    text: cocoindex.DataSlice[str],
) -> cocoindex.DataSlice[NDArray[np.float32]]:
    """
    Embed đoạn text thành vector bằng SentenceTransformer.
    transform_flow cho phép dùng lại hàm này ở cả bước indexing
    lẫn bước embed query khi search.
    """
    return text.transform(
        cocoindex.functions.SentenceTransformerEmbed(
            model="sentence-transformers/all-MiniLM-L6-v2"
        )
    )


# --- Bước 2: Định nghĩa indexing flow ---
@cocoindex.flow_def(name="CodeEmbedding")
def code_embedding_flow(flow_builder: cocoindex.FlowBuilder, data_scope: cocoindex.DataScope):
    """
    Flow đánh index mã nguồn: đọc file, chunk, embed, lưu vào Postgres.
    """
    # Dùng kỹ thuật symlink workspace để gộp nhiều source directories vào một LocalFile source
    data_scope["files"] = flow_builder.add_source(
        cocoindex.sources.LocalFile(path="/tmp/workspace")
    )

    code_embeddings = data_scope.add_collector()

    with data_scope["files"].row() as file:
        file["lang"] = file["filename"].transform(cocoindex.functions.DetectProgrammingLanguage())

        # Cắt file code thành các chunk dùng Tree-sitter tích hợp trong SplitRecursively
        file["chunks"] = file["content"].transform(
            cocoindex.functions.SplitRecursively(),
            language=file["lang"],
            chunk_size=1000,
            chunk_overlap=200,
        )

        with file["chunks"].row() as chunk:
            # Gọi transform_flow code_to_embedding thay vì khai báo SentenceTransformerEmbed inline
            # Cách này cho phép tái dùng cùng logic embed tại query time
            chunk["embedding"] = chunk["text"].call(code_to_embedding)

            code_embeddings.collect(
                filename=file["filename"],
                lang=file["lang"],
                text=chunk["text"],
                embedding=chunk["embedding"],
            )

    code_embeddings.export(
        "code_embeddings",
        cocoindex.targets.Postgres(),
        primary_key_fields=["filename", "text"],
        vector_indexes=[
            cocoindex.VectorIndexDef(
                field_name="embedding",
                metric=cocoindex.VectorSimilarityMetric.COSINE_SIMILARITY,
            )
        ],
    )


# --- Bước 3: Connection pool tới Postgres ---
@functools.cache
def _connection_pool() -> ConnectionPool:
    return ConnectionPool(os.environ["COCOINDEX_DATABASE_URL"])


TOP_K = 5

# --- Bước 4: Đăng ký semantic search handler với CocoIndex ---
# @code_embedding_flow.query_handler() đăng ký hàm này như một "named query handler"
# CocoIndex tự quản lý metadata và cho phép CocoInsight visualize kết quả
@code_embedding_flow.query_handler(
    result_fields=cocoindex.QueryHandlerResultFields(
        embedding=["embedding"], score="score"
    )
)
def search(query: str) -> cocoindex.QueryOutput:
    """
    Semantic search handler built-in của CocoIndex.
    Tự động:
      1. Embed query text bằng cùng model đã dùng khi index (code_to_embedding.eval)
      2. Lấy đúng tên bảng qua cocoindex.utils.get_target_default_name
      3. Chạy vector similarity query và trả về QueryOutput chuẩn CocoIndex
    """
    # Lấy đúng tên bảng Postgres đã export (tránh hardcode)
    table_name = cocoindex.utils.get_target_default_name(code_embedding_flow, "code_embeddings")

    # Embed câu query dùng cùng transform_flow với lúc indexing
    query_vector = code_to_embedding.eval(query)

    with _connection_pool().connection() as conn:
        register_vector(conn)
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT filename, lang, text, embedding, embedding <=> %s AS distance
                FROM {table_name}
                ORDER BY distance
                LIMIT %s
                """,
                (query_vector, TOP_K),
            )
            return cocoindex.QueryOutput(
                query_info=cocoindex.QueryInfo(
                    embedding=query_vector,
                    similarity_metric=cocoindex.VectorSimilarityMetric.COSINE_SIMILARITY,
                ),
                results=[
                    {
                        "filename": row[0],
                        "lang":     row[1],
                        "text":     row[2],
                        "embedding": row[3],
                        "score":    1.0 - row[4],   # distance -> similarity score
                    }
                    for row in cur.fetchall()
                ],
            )
