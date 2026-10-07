# Core API (13 game) — bản đã fix, deploy Render

HTTP + WebSocket chạy chung 1 cổng (`$PORT`).

## Deploy lên Render
1. Push toàn bộ source này lên GitHub (để `app.py`, `core/`, `render.yaml` nằm ở thư mục gốc).
2. Render → New → Blueprint (dùng `render.yaml`), hoặc New Web Service:
   - Root Directory: `.`
   - Build: `pip install -r requirements.txt`
   - Start: `python app.py`
   - Health Check Path: `/healthz`
   - Env: `PYTHON_VERSION=3.11.9`, `PYTHONUNBUFFERED=1`, `DATA_DIR=/tmp/ppp-data`

## Đường dẫn
- `/` dashboard trực tiếp (tự cập nhật 15s, không reload trang)
- `/healthz`, `/api/status`, `/api/all`, `/api/<game>/current`
- API từng game: `/<game>/api/...` (xem dashboard)
- WebSocket: `wss://<domain>/ws/<game>` — gửi khi có phiên mới + nhịp 30s khi không có dữ liệu mới

## Đã fix
- HTTP chạy trong thread pool → không khóa vòng lặp WebSocket (hết chậm/treo).
- Monitor đọc dữ liệu ngay trong process, không tự gọi HTTP vào chính mình.
- `get_current` gom mọi kiểu biến của 13 core + fallback lịch sử đã lưu → giảm null.
- Ngưỡng trạng thái rộng (ok <100s) → không báo "chậm" oan.
- Watchdog khởi động lại worker chết (tối đa 2 lần/game, cách 10 phút).
- Backoff tăng dần cho sumclub / sao789 + chống spam log.
- Keep-alive `RENDER_EXTERNAL_URL` mỗi 10 phút; response HTTP được gzip + hỗ trợ ETag để giảm outbound bandwidth.

## Còn lại (cần dữ liệu mới, không sửa được bằng code)
- **sumclub**: WebSocket nhà cung cấp trả HTTP 400 → cần URL/hub + header/token mới.
- **sao789**: API auth trả HTML thay vì JSON → cần token/endpoint auth mới.
Hai game này vẫn giữ endpoint, tự thử lại và không làm ảnh hưởng 11 game còn lại.


## Tối ưu Render Hobby
- API JSON/HTML tự gzip khi client hỗ trợ.
- Client revalidate với ETag sẽ nhận `304 Not Modified` nếu dữ liệu chưa đổi.
- Giảm polling nội bộ ở các core HTTP từ 5s xuống 10s (Hitclub 8s, Sunwin/789 Sicbo 10s); WebSocket realtime vẫn giữ.
- Dashboard poll 15s; WebSocket chỉ heartbeat 30s khi không có dữ liệu mới.
- Không đổi endpoint, field hay cấu trúc history/API.
