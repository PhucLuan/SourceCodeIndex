import json
import os
from dataclasses import dataclass, asdict
from typing import List, Dict, Optional

@dataclass
class EmbedderProfile:
    name: str                    # "code", "general", "doc", v.v.
    display_name: str            # Tên hiển thị trên giao diện Streamlit
    model_id: str                # HuggingFace Model ID hoặc đường dẫn model
    table_name: str              # Bảng lưu trữ trong PostgreSQL
    source_extensions: List[str] # Các đuôi file được áp dụng profile này
    description: str = ""        # Mô tả ngắn gọn về model
    max_tokens: int = 384        # Context window tối đa
    # Prefix instruction cho từng mô hình (để trống nếu model không cần)
    document_prefix: str = ""    # Prefix thêm vào văn bản khi đánh index (document)
    query_prefix: str = ""       # Prefix thêm vào câu hỏi khi tìm kiếm (query)
    trust_remote_code: bool = False  # True nếu model yêu cầu custom code từ Hub

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "EmbedderProfile":
        # Tương thích ngược: bỏ qua các key không hợp lệ từ file JSON cũ
        valid_keys = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in valid_keys}
        return cls(**filtered)

# Danh sách các Profiles được định nghĩa sẵn
DEFAULT_PROFILES: Dict[str, EmbedderProfile] = {
    "general": EmbedderProfile(
        name="general",
        display_name="General — all-mpnet-base-v2",
        model_id="sentence-transformers/all-mpnet-base-v2",
        table_name="code_embeddings",
        source_extensions=["py","ts","tsx","js","cs","html","css","scss","less"],
        description="Mô hình đa dụng, nhẹ chạy tốt trên CPU, context window 384 tokens.",
        max_tokens=384,
        document_prefix="",
        query_prefix="",
        trust_remote_code=False,
    ),
    "code": EmbedderProfile(
        name="code",
        display_name="Code — nomic-embed-text-v1.5 (Mặc định)",
        model_id="nomic-ai/nomic-embed-text-v1.5",
        table_name="code_embeddings",
        source_extensions=["py","ts","tsx","js","cs","html","css","scss","less"],
        description="Mô hình nhúng dài 8192 tokens chuẩn Native ổn định, chạy mượt trên CPU.",
        max_tokens=8192,
        # Nomic yêu cầu instruction prefix để phân biệt document vs query
        document_prefix="search_document: ",
        query_prefix="search_query: ",
        trust_remote_code=True,
    ),
    # Chuẩn bị cho Phase 4 khi mở rộng tài liệu Excel, Word, Markdown, v.v.
    # "doc": EmbedderProfile(
    #     name="doc",
    #     display_name="Document — all-mpnet-base-v2 (Word, Excel, PDF)",
    #     model_id="sentence-transformers/all-mpnet-base-v2",
    #     table_name="doc_embeddings",
    #     source_extensions=["md","txt","docx","xlsx","pdf"],
    #     description="Mô hình tối ưu cho tài liệu văn bản thông thường.",
    #     max_tokens=384,
    # )
}

CONFIG_FILE = "active_profile.json"

def load_active_profile() -> EmbedderProfile:
    """Đọc cấu hình active profile từ file, mặc định trả về 'code'."""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return EmbedderProfile.from_dict(data)
        except Exception:
            pass
    # Mặc định ban đầu dùng code profile (Jina Code v2)
    return DEFAULT_PROFILES["code"]

def save_active_profile(profile: EmbedderProfile) -> None:
    """Lưu cấu hình active profile xuống file để khôi phục sau khi restart."""
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(profile.to_dict(), f, indent=4, ensure_ascii=False)
    except Exception as e:
        import sys
        sys.stderr.write(f"[ERROR] Không thể lưu active profile: {e}\n")
        sys.stderr.flush()
