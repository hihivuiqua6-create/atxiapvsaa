# Core API (Flask + WebSocket) — deploy Render.com

Chạy HTTP và WebSocket trên **cùng 1 cổng** (`$PORT` do Render cấp).

## Deploy nhanh
1. Đẩy repo này lên GitHub.
2. Vào Render → **New → Blueprint** → chọn repo (Render đọc `render.yaml` ở thư mục gốc).
   - Hoặc **New → Web Service** thủ công:
     - Root Directory: `server-python`
     - Build: `pip install -r requirements.txt`
     - Start: `python app.py`
     - Health check path: `/healthz`
3. Deploy. URL dạng `https://<ten-service>.onrender.com`.

## Endpoint
- `/` dashboard, `/api/status`, `/api/ws`, `/healthz`
- API game: `/sunwin/api/sicbo`, `/b52/api/tx`, `/789/api/sicbo`, ... (xem `/api/status`)
- WebSocket: `wss://<ten-service>.onrender.com/ws/<game>`

## Lưu ý ổn định
- Gói Free của Render sẽ **ngủ sau 15 phút** không có request → dùng UptimeRobot ping `/healthz` mỗi 5 phút, hoặc lên gói Starter để chạy 24/7.
- Dữ liệu lịch sử lưu ở `DATA_DIR` (mặc định `/tmp/ppp-data`) và sẽ mất khi service restart. Muốn giữ lâu dài thì gắn Persistent Disk và đặt `DATA_DIR` vào đường dẫn disk.

## Chạy local
```bash
cd server-python
pip install -r requirements.txt
PORT=5000 python app.py
```
