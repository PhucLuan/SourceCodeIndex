import os
import cocoindex

@cocoindex.flow_def(name="CodeEmbedding")
def code_embedding_flow(flow_builder: cocoindex.FlowBuilder, data_scope: cocoindex.DataScope):
    # Dùng kĩ thuật symlink workspace để gộp chung nhiều source directories lại thành một LocalFile source duy nhất.
    data_scope["files"] = flow_builder.add_source(
        cocoindex.sources.LocalFile(path="/tmp/workspace")
    )
    
    # Định nghĩa collector để gom data trước khi lưu xuống PostgreSQL
    code_embeddings = data_scope.add_collector()
    
    with data_scope["files"].row() as file:
        file["lang"] = file["filename"].transform(cocoindex.functions.DetectProgrammingLanguage())
        
        # Cắt file code thành các chunk sử dụng Tree-sitter được tích hợp trong SplitRecursively của CocoIndex
        file["chunks"] = file["content"].transform(
            cocoindex.functions.SplitRecursively(),
            language=file["lang"],
            chunk_size=1000,
            chunk_overlap=200
        )
        
        with file["chunks"].row() as chunk:
            # Tạo vector embedding bằng một model chuẩn chạy trên local CPU.
            chunk["embedding"] = chunk["text"].transform(
                cocoindex.functions.SentenceTransformerEmbed(
                    model="sentence-transformers/all-MiniLM-L6-v2"
                )
            )
            
            # Đẩy data vào collector
            code_embeddings.collect(
                filename=file["filename"],
                lang=file["lang"],
                text=chunk["text"],
                embedding=chunk["embedding"]
            )
            
    # Export bảng từ collector xuống database Postgres
    code_embeddings.export(
        "code_embeddings_table",
        cocoindex.targets.Postgres(table_name="code_embeddings_table"),
        primary_key_fields=["filename", "text"], # dùng tổ hợp tên file và nội dung text làm khoá chính
        vector_indexes=[
            cocoindex.VectorIndexDef(
                field_name="embedding", 
                metric=cocoindex.VectorSimilarityMetric.COSINE_SIMILARITY
            )
        ]
    )
