# Hướng dẫn Khởi chạy Ứng dụng Semantic Search Codebase

Ứng dụng của bạn đã được viết xong hoàn chỉnh bám sát theo triết lý "NotebookLM cho codebase". 

## 1. Tính năng nổi bật
- **Hoạt động bằng Docker Engine**: Khắc phục lỗi tương thích phiên bản Python 3.9 của bạn, toàn bộ code Streamlit và `cocoindex` (+pgvector database) được đóng gói và chạy ổn định bên trong môi trường Docker Container với **Python 3.11**.
- **UI Trực quan bằng Streamlit**: Cung cấp sidebar để quản lý (thêm/xoá) các thư mục và cập nhật index.
- **Tích hợp Cocoindex**: Cắt và chia cấu trúc code (`Tree-sitter`), tạo embedding tự động.
- **Truy vấn RAG Semantics cực mạnh**: Cho phép gọi Local Model của bạn qua Ollama (hoặc OpenAI/Gemini) và giải đáp dựa trên chính các dòng file/code gốc. Cấu hình Ollama tự động ping về localhost của Windows.

## 2. Cấu trúc Source Code
Tất cả các tài nguyên đều được sinh ra ở `c:\LEARN\SourceCodeIndex`.
- `Dockerfile` & `docker-compose.yml`: Tự động build môi trường và trỏ các volume cần thiết.
- `app.py`: Giao diện chính của Chat & Controls.
- `indexer_flow.py`: Cấu hình Data Pipeline của thư viện con `cocoindex`.
- `rag.py`: File trích xuất dữ liệu từ VectorDB và invoke Model qua Langchain.

## 3. Cách Khởi chạy
Bật Terminal (của PowerShell/CMD) ở thư mục `c:\LEARN\SourceCodeIndex`, sau đó chạy:

```bash
docker-compose up -d --build
```
*Lưu ý: Lần đầu chạy sẽ mất một thời gian vì docker phải build image kéo gói model embedding.*

Sau khi Terminal báo `Running/Started` hoàn tất, vào trình duyệt của bạn với đường dẫn gốc:
👉 `http://localhost:8501`

## 4. Cách Sử Dụng UI
1. **Source Mới**: Ổ đĩa C của bạn đã được mount vào trong ứng dụng. Bạn hãy trỏ đường dẫn tuyệt đối với prefix `/host_c`. 
   *Ví dụ thay vì nhập `c:\MyProjects\Test`, hãy nhập `/host_c/MyProjects/Test`.*
2. Bấm "Thêm Source".
3. Ấn nút "🔄 Cập nhật Index" (bạn sẽ xem terminal stream trên màn hình).
4. Ở thân trang (Main Chat), thử test câu hỏi ngẫu nhiên.
5. Cập nhật tên của mô hình đang chạy trên Ollama Desktop (vd: `llama3`, `qwen`, `gemma`) vào ô config để AI phản hồi lại.
