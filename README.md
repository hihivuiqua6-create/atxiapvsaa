# Core API (13 game) — bản đã fix, deploy Render

HTTP + WebSocket chạy chung 1 cổng (`$PORT`).

## Deploy lên Render
1. Push repo (thư mục `server-python/`) lên GitHub.
2. Render → New → Blueprint (dùng `render.yaml`), hoặc New Web Service:
   - Root Directory: `server-python`
   - Build: `pip install -r requirements.txt`
   - Start: `python app.py`
   - Health Check Path: `/healthz`
   - Env: `PYTHON_VERSION=3.11.9`, `PYTHONUNBUFFERED=1`, `DATA_DIR=/tmp/ppp-data`

## Đường dẫn
- `/` dashboard trực tiếp (tự cập nhật 5s, không reload trang)
- `/healthz`, `/api/status`, `/api/all`, `/api/<game>/current`
- API từng game: `/<game>/api/...` (xem dashboard)
- WebSocket: `wss://<domain>/ws/<game>` — gửi khi có phiên mới + nhịp 15s

## Đã fix
- HTTP chạy trong thread pool → không khóa vòng lặp WebSocket (hết chậm/treo).
- Monitor đọc dữ liệu ngay trong process, không tự gọi HTTP vào chính mình.
- `get_current` gom mọi kiểu biến của 13 core + fallback lịch sử đã lưu → giảm null.
- Ngưỡng trạng thái rộng (ok <100s) → không báo "chậm" oan.
- Watchdog khởi động lại worker chết (tối đa 2 lần/game, cách 10 phút).
- Backoff tăng dần cho sumclub / sao789 + chống spam log.
- Keep-alive `RENDER_EXTERNAL_URL` mỗi 4 phút để service free không bị ngủ.

## Còn lại (cần dữ liệu mới, không sửa được bằng code)
- **sumclub**: WebSocket nhà cung cấp trả HTTP 400 → cần URL/hub + header/token mới.
- **sao789**: API auth trả HTML thay vì JSON → cần token/endpoint auth mới.
Hai game này vẫn giữ endpoint, tự thử lại và không làm ảnh hưởng 11 game còn lại.
