# Local Codebase Indexing & Semantic Search Application

Ứng dụng của bạn sẽ hoạt động tương tự như một "NotebookLM dành riêng cho mã nguồn", cho phép bạn trỏ tới các thư mục mã nguồn khác nhau ở local, tiến hành đánh index, và sau đó tìm kiếm theo ngữ nghĩa (semantic search) hoặc sử dụng tính năng RAG để trả lời câu hỏi dựa trên ngữ cảnh lấy từ mã nguồn.

## Đề xuất Kiến trúc & Giải pháp

### Tech Stack
- **Frontend / UI**: [Streamlit](https://streamlit.io/) để xây dựng giao diện nhanh chóng. Thích hợp để tạo các app dạng chat/RAG có sidebar quản lý nguồn dữ liệu.
- **Indexing Engine**: `cocoindex` theo yêu cầu của bạn, được sử dụng để phân tích và đánh index incremental các file code (sẽ sử dụng Tree-sitter để phân tích cấu trúc nếu có).
- **LLM / Vector Search / RAG**: `langchain` (để kết nối LLM generate câu trả lời với context) và có thể sử dụng các model như OpenAI, Anthropic, hoặc Ollama (chạy model offline trên local của bạn).
- **Database**: `cocoindex` hiện tại yêu cầu backend lưu trữ vector và state (thường là PostgreSQL với extention `pgvector`).

### Cấu trúc file dự kiến
- `app.py`: Chứa giao diện Streamlit (UI), cho phép thêm/kiểm soát các đường dẫn nguồn (sources) và giao diện chat (search).
- `indexer.py`: Chứa logic định nghĩa `cocoindex` dataflow. Tiếp nhận các danh sách thư mục cần quét, tiến hành chunking và embedding mã nguồn.
- `search_rag.py`: Xử lý Langchain. Dùng query của user để chuyển thành embedding, query từ vector DB các đoạn code liên quan, và đưa vào Prompt cho LLM trả lời.
- `docker-compose.yml`: (Tùy chọn) Chứa cấu hình PostgreSQL + pgvector để bạn có thể bật database cục bộ với 1 lệnh nhanh chóng.
- `requirements.txt`: Các thư viện Python cần thiết (`streamlit`, `cocoindex`, `langchain`, `langchain-openai`...).

## Các module chính

### 1. Quản lý Nguồn dữ liệu (Source Management)
Ở giao diện Streamlit, bạn có thể nhập đường dẫn tuyệt đối (absolute path) của các thư mục trên máy (VD: `C:\MyProjects\Frontend`, `C:\MyProjects\Backend`). 
Hệ thống sẽ lưu danh sách các thư mục được "kích hoạt". Khi ấn "Cập nhật Index", file `indexer.py` sẽ được chạy với cấu hình `DataScope` tương ứng với các thư mục này.

### 2. Xây dựng Index (CocoIndex Data Flow)
CocoIndex sẽ quét đệ quy các thư mục được chỉ định.
- Lọc theo định dạng các file liên quan (.py, .ts, .js, .java...).
- Dùng `SplitRecursively()` để cắt mã nguồn theo cấu trúc syntax.
- Dùng model Embeddings (VD: `sentence-transformers` hoặc `OpenAIEmbeddings`) để tạo vector cho từng đoạn mã.
- Lưu xuống Vector Table.

### 3. Truy vấn & Trả lời (Semantic Search & RAG)
Khi bạn nhập câu hỏi (VD: *"Tìm tất cả những nơi liên quan tới chức năng tạo tài sản"*):
- Hệ thống query Postgres VectorDB lấy ra top N đoạn code liên quan nhất (kèm file path & line number).
- Langchain gộp các đoạn code này lại thành "Context" và nhồi vào prompt cho LLM.
- LLM tổng hợp và đưa ra câu trả lời chi tiết chỉ ra nơi định nghĩa, file nào, logic chạy ra sao dựa trên Context.

## User Review Required

> [!WARNING]
> **Yêu cầu về Database của `cocoindex`**
> Thư viện `cocoindex` hoạt động dựa trên cơ chế Dataflow Model, và hiện tại engine của nó yêu cầu sử dụng backend là **PostgreSQL có cài đặt sẵn plugin `pgvector`** để quản lý trạng thái incremental và lưu trữ dữ liệu vector.
> Việc sử dụng tính năng "chỉ scan file local mà không cần setup DB" hoàn toàn (như sqlite hay duckdb) với `cocoindex` có thể bị hạn chế. Đòi hỏi bạn phải chạy PostgreSQL service.

> [!IMPORTANT]
> **Về cấu hình LLM Model (Mô hình ngôn ngữ):**
> Để thực hiện tính năng RAG, bạn muốn sử dụng API trả phí (như GPT-4o của OpenAI, Claude) qua API Key, hay bạn muốn chạy hoàn toàn offline bằng một Open Source model trên máy của bạn (Ví dụ dùng Ollama)? 

## Open Questions

1. **Bạn có sẵn môi trường Docker trên máy (hay đã có sẵn PostgreSQL + pgvector) không?** Nếu dùng Docker, tôi sẽ tạo cho bạn một file `docker-compose.yml` để bạn bật Vector Database với chỉ 1 câu lệnh `docker-compose up -d`.
2. **Loại LLM nào bạn dự định dùng cho ứng dụng này?** OpenAI API hay Ollama local model?
3. **Thư mục lưu trữ dự án:** Tôi sẽ tạo toàn bộ source code của app này ở folder `c:\LEARN\SourceCodeIndex` như chỉ định, bạn đồng ý chứ?

## Verification Plan
1. Viết code Streamlit UI và các modules Python.
2. Viết file `docker-compose` setup database.
3. Chạy `streamlit run app.py` và kiểm tra giao diện.
4. Yêu cầu bạn cấu hình Docker, cài requirements và chạy thử ứng dụng.
