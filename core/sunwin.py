# server.py
import asyncio
import websockets
import json
import threading
import time
from datetime import datetime, timedelta
from flask import Blueprint, jsonify
from flask_cors import CORS


CAU_LEN = 15


def to_tx(ket_qua):
    """Chuyển ket_qua sang ký tự T/X/? (nếu không có tài/xỉu)."""
    if not ket_qua:
        return "?"
    k = str(ket_qua).strip().lower()
    if "tà" in k or "tai" in k:
        return "T"
    if "xỉ" in k or "xiu" in k or "bão" in k or "bao" in k:
        return "X"
    return "?"


def build_cau(history):
    """Trả về chuỗi cau (dài tối đa CAU_LEN, mới nhất ở cuối) từ history mới->cũ."""
    seq = "".join(to_tx(e.get("ket_qua")) for e in (history or [])[:CAU_LEN])
    return seq[::-1]

def _st(v):
    return None if v is None else str(v)


def latest_payload(latest, cau):
    """Tạo payload kết quả mới nhất theo schema dice1/dice2/dice3/dice."""
    xuc = latest.get("xuc_xac") or []
    d1 = xuc[0] if len(xuc) > 0 else None
    d2 = xuc[1] if len(xuc) > 1 else None
    d3 = xuc[2] if len(xuc) > 2 else None
    dice = None if (d1 is None or d2 is None or d3 is None) else f"{d1}-{d2}-{d3}"
    return {
        "phien": latest.get("phien"),
        "dice1": _st(d1),
        "dice2": _st(d2),
        "dice3": _st(d3),
        "dice": dice,
        "tong": _st(latest.get("tong")),
        "ket_qua": latest.get("ket_qua"),
        "cau": cau,
        "last_update": latest.get("last_update"),
    }


def decorate_history(history, limit=None):
    """Trả về bản copy history (mới->cũ) theo schema dice1/dice2/dice3/dice, kèm 'cau'
    là chuỗi T/X cũ->mới của 15 phiên kết thúc ở phiên đó (mới nhất ở cuối)."""
    target = (history or [])[:limit] if limit else (history or [])
    result = []
    for i, e in enumerate(target):
        window = "".join(to_tx(x.get("ket_qua")) for x in (history or [])[i:i + CAU_LEN])
        xuc = e.get("xuc_xac") or []
        d1 = xuc[0] if len(xuc) > 0 else None
        d2 = xuc[1] if len(xuc) > 1 else None
        d3 = xuc[2] if len(xuc) > 2 else None

        def st(v):
            return None if v is None else str(v)

        dice = None if (d1 is None or d2 is None or d3 is None) else f"{d1}-{d2}-{d3}"
        row = {
            "phien": e.get("phien"),
            "dice1": st(d1),
            "dice2": st(d2),
            "dice3": st(d3),
            "dice": dice,
            "tong": st(e.get("tong")),
            "ket_qua": e.get("ket_qua"),
            "cau": window[::-1],
        }
        if e.get("last_update") is not None:
            row["last_update"] = e.get("last_update")
        result.append(row)
    return result


import os
import signal
import sys
import socket
import requests
import re

# Khôi phục console trên Windows nếu một module trước đó đã thay/đóng sys.stdout.
# Một số module khác trong source bọc lại sys.stdout bằng TextIOWrapper; khi wrapper
# cũ bị GC, nó có thể đóng luôn console stream dùng chung.
if sys.platform == "win32":
    try:
        if getattr(sys.stdout, "closed", False):
            sys.stdout = open("CONOUT$", "w", encoding="utf-8", errors="replace")
    except Exception:
        try:
            sys.stdout = open("CONOUT$", "w", encoding="utf-8", errors="replace")
        except Exception:
            pass

bp = Blueprint('gamesunwin', __name__)

# Global variables
current_result = {
    "phien": None,
    "xuc_xac": [None, None, None],
    "tong": None,
    "ket_qua": "",
    "last_update": ""
}

current_session_id = None
_last_message_time = None
reconnect_delay = 2.5  # seconds
start_time = time.time()

# ---- TX (cập nhật bằng code sunwin_crawler.py) ----
TX_FILE = "sunwin-tx"
tx_history = []
_committed_tx = set()
_tx_lock = threading.Lock()

# ---- Sicbo (gộp từ sicbosun/get.py) ----
SICBO_SOURCE_URL = "https://api.wsktnus8.net/v2/history/getLastResult"
SICBO_PARAMS = {
    "gameId": "ktrng_3979",
    "size": 1,
    "tableId": "39791215743193",
    "curPage": 1
}
SICBO_POLL_INTERVAL = 5

sicbo_result = {
    "phien": None,
    "xuc_xac": [None, None, None],
    "tong": None,
    "ket_qua": "",
    "last_update": ""
}

sicbo_history = []
MAX_HISTORY = 100

# Hàm lấy thời gian Việt Nam (UTC+7)
def get_vietnam_time():
    utc7_time = datetime.utcnow() + timedelta(hours=7)
    return utc7_time.strftime("%d-%m-%Y %H:%M:%S") + " UTC+7"

def parse_token_data(token_text):
    """Parse token data từ file token.txt"""
    try:
        # Tìm và trích xuất info JSON
        info_match = re.search(r'"info"\x07([^"]+?)"?', token_text)
        if info_match:
            info_str = info_match.group(1)
            # Làm sạch chuỗi JSON
            info_str = info_str.replace('\x04', '').replace('\x07', '').replace('\x05', '').replace('\x06', '')
            info_data = json.loads(info_str)
            return info_data
        
        # Nếu không tìm thấy info, tìm trực tiếp JSON
        json_match = re.search(r'\{[^{}]*"ipAddress"[^{}]*\}', token_text)
        if json_match:
            return json.loads(json_match.group())
        
        return None
    except Exception as e:
        print(f"[❌] Lỗi parse token: {e}")
        return None

def load_token():
    """Load token từ file token.txt (cùng thư mục với script)"""
    try:
        token_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'token.txt')
        with open(token_file, 'r', encoding='utf-8') as f:
            token_data = f.read().strip()
        
        if not token_data:
            print("[❌] File token.txt trống")
            return None
        
        parsed_data = parse_token_data(token_data)
        if parsed_data:
            print("[✅] Đã load token từ token.txt")
            return parsed_data
        else:
            print("[❌] Không thể parse token từ token.txt")
            return None
            
    except FileNotFoundError:
        print("[❌] Không tìm thấy file token.txt")
        return None
    except Exception as e:
        print(f"[❌] Lỗi đọc token.txt: {e}")
        return None

# Load token data
TOKEN_DATA = load_token()
if TOKEN_DATA:
    print(f"👤 Đang dùng token từ token.txt của: {TOKEN_DATA.get('username', 'Unknown')}")

# Cấu hình websocket TX — lấy đúng từ 1.js (đã kiểm chứng hoạt động)
WS_URL_TOKEN = "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJhbW91bnQiOjAsInVzZXJuYW1lIjoiU0NfYXBpc3Vud2luMTIzIn0.hgrRbSV6vnBwJMg9ZFtbx3rRu9mX_hZMZ_m5gMNhkw0"
WEBSOCKET_URL = f"wss://websocket.azhkthg1.net/websocket?token={WS_URL_TOKEN}"

# ---- TX: config từ sunwin_crawler.py (account SC_thiennhanvn113) ----
TX_WS_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Origin": "https://web.sunwin.date"
}
TX_INITIAL_MESSAGES = [
    [1, "MiniGame", "SC_thiennhanvn113", "nhan123", {
        "info": json.dumps({
            "ipAddress": "14.172.129.70",
            "wsToken": (
                "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9"
                ".eyJnZW5kZXIiOjAsImNhblZpZXdTdGF0IjpmYWxzZSwiZGlzcGxheU5hbWUiOiJ0aGllbm5oYW52bnp0YyIsImJvdCI6MCwiaXNNZXJjaGFudCI6ZmFsc2UsInZlcmlmaWVkQmFua0FjY291bnQiOmZhbHNlLCJwbGF5RXZlbnRMb2JieSI6ZmFsc2UsImN1c3RvbWVySWQiOjM1ODY2MTg2OSwiYWZmSWQiOiJTdW53aW4iLCJiYW5uZWQiOmZhbHNlLCJicmFuZCI6InN1bi53aW4iLCJlbWFpbCI6IiIsInRpbWVzdGFtcCI6MTc4NDY4NjMyODAxOCwibG9ja0dhbWVzIjpbXSwiYW1vdW50IjowLCJsb2NrQ2hhdCI6ZmFsc2UsInBob25lVmVyaWZpZWQiOmZhbHNlLCJpcEFkZHJlc3MiOiIyNDA1OjQ4MDI6ZTZlZTo1ZmMwOmQzMzc6NzRjOjM2ODg6ZjE2YiIsIm11dGUiOmZhbHNlLCJhdmF0YXIiOiJodHRwczovL2ltYWdlcy5zd2luc2hvcC5uZXQvaW1hZ2VzL2F2YXRhci9hdmF0YXJfMTIucG5nIiwicGxhdGZvcm1JZCI6NSwidXNlcklkIjoiZTVmZmExNDYtODEzMi00MzZlLWE5NjQtZDgyNjQ3ODcyMTc3IiwiZW1haWxWZXJpZmllZCI6bnVsbCwicmVnVGltZSI6MTc4NDY4NjI1NzU0NCwicGhvbmUiOiIiLCJkZXBvc2l0IjpmYWxzZSwidXNlcm5hbWUiOiJTQ190aGllbm5oYW52bjExMyJ9"
                ".7x8Ls-miv6iRHIEpwmOOWx9ct6s1LVNRe9kU-5_rzMQ"
            ),
            "locale": "vi",
            "userId": "e5ffa146-8132-436e-a964-d82647872177",
            "username": "SC_thiennhanvn113",
            "timestamp": 1784686328029,
            "refreshToken": "88b0191b5135448e960615eb5d734844.e2b75a82d398412788e9d38aa0ba9fb6"
        }),
        "signature": "8935544C6D01C51766ECF42A1BA498613B591FB6A464581F1EB706E9FFAB61E9C209C3682D771216F53FE5397C7AA9FA4BC374438943DE653A5CA5E9E28740B888C241D39B22ED9140FE3364C602EBD55438C785E56B7750A9D923D8505CECA809E00409FD194EBE5819B8A13861D6CE80587427F8284C66D1E85C7981583D21"
    }],
    [6, "MiniGame", "lobbyPlugin", {"cmd": 10001}]
]
TX_HEARTBEAT_MSG = [6, "MiniGame", "taixiuPlugin", {"cmd": 1005}]

# ---- Sicbo: fetch & transform (gộp từ sicbosun/get.py) ----
def fetch_sicbo():
    """Gọi API gốc để lấy kết quả sicbo mới nhất"""
    try:
        response = requests.get(SICBO_SOURCE_URL, params=SICBO_PARAMS, timeout=10)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        handle_error("Gọi API sicbo", e)
        return None

def transform_sicbo(raw_data):
    """Chuyển đổi dữ liệu sicbo từ API gốc sang format chuẩn"""
    try:
        result_list = raw_data.get("data", {}).get("resultList", [])
        if not result_list:
            return None

        latest = result_list[0]
        faces = latest.get("facesList", [0, 0, 0])
        total = sum(faces)

        # Ưu tiên kiểm tra Bão trước
        if len(faces) >= 3 and faces[0] == faces[1] == faces[2]:
            ket_qua = "Bão"
        else:
            # Quy tắc: 4-10 là Xỉu, 11-17 là Tài
            ket_qua = "Tài" if total >= 11 else "Xỉu"

        return {
            "phien": latest.get("gameNum", ""),
            "xuc_xac": [faces[0] if len(faces) > 0 else None,
                        faces[1] if len(faces) > 1 else None,
                        faces[2] if len(faces) > 2 else None],
            "tong": total,
            "ket_qua": ket_qua,
            "last_update": get_vietnam_time()
        }
    except Exception as e:
        handle_error("Transform sicbo", e)
        return None

def sicbo_poll_loop():
    """Poll sicbo định kỳ và lưu kết quả mới nhất vào history"""
    global sicbo_result
    while True:
        raw_data = fetch_sicbo()
        if raw_data:
            transformed = transform_sicbo(raw_data)
            if transformed:
                sicbo_result = transformed
                if not sicbo_history or sicbo_history[0]["phien"] != transformed["phien"]:
                    sicbo_history.insert(0, transformed)
                    del sicbo_history[MAX_HISTORY:]
                    _ssave("sunwin-sicbo", sicbo_history)
                print(f"[🎲] Sicbo phiên {transformed['phien']}: "
                      f"{'-'.join(str(x) for x in transformed['xuc_xac'])} "
                      f"= {transformed['tong']} ({transformed['ket_qua']}) | cau={build_cau(sicbo_history)}")
        time.sleep(SICBO_POLL_INTERVAL)

def get_network_info():
    """Lấy thông tin mạng"""
    try:
        hostname = socket.gethostname()
        local_ip = socket.gethostbyname(hostname)
        
        try:
            response = requests.get('https://api.ipify.org?format=json', timeout=5)
            public_ip = response.json()['ip']
        except:
            public_ip = None
            
        return {'localIP': local_ip, 'publicIP': public_ip}
    except Exception as e:
        print(f"Lỗi lấy network info: {e}")
        return {'localIP': '127.0.0.1', 'publicIP': None}

def handle_error(context, error):
    """Xử lý lỗi"""
    error_msg = f"Lỗi - {context}: {str(error)}"
    print(f"[❌] {error_msg}")
    return error_msg

def _commit_tx(sid, d1, d2, d3, source):
    """Commit 1 phiên TX (deep-scan, dedupe theo sid) — logic từ sunwin_crawler.py."""
    global current_result
    if sid is None or sid in _committed_tx:
        return
    d1 = int(d1); d2 = int(d2); d3 = int(d3)
    total = d1 + d2 + d3
    result = "Tài" if total >= 11 else "Xỉu"
    from datetime import datetime
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    _committed_tx.add(sid)

    now = get_vietnam_time()
    with _tx_lock:
        current_result = {
            "phien": str(sid),
            "xuc_xac": [d1, d2, d3],
            "tong": total,
            "ket_qua": result,
            "last_update": now
        }
        entry = {
            "phien": str(sid),
            "xuc_xac": [d1, d2, d3],
            "tong": total,
            "ket_qua": result,
            "last_update": now
        }
        # history mới->cũ (index 0 là mới nhất), cap 100
        tx_history.insert(0, entry)
        del tx_history[100:]
        _ssave(TX_FILE, tx_history)

    print(f"[🎲 WIN] Phiên {sid} | {d1}-{d2}-{d3} = {total} ({result}) | Nguồn: {source} | cau={build_cau(tx_history)}")


def _extract_tx_from_payload(obj, source='auto-recovery'):
    """Deep scan payload tìm kết quả (logic từ sunwin_crawler.py)."""
    if obj is None:
        return
    if isinstance(obj, dict):
        sid = obj.get('sid')
        if sid and obj.get('d1') is not None and obj.get('d2') is not None and obj.get('d3') is not None:
            _commit_tx(sid, obj['d1'], obj['d2'], obj['d3'], source)
        if sid and isinstance(obj.get('dices'), list) and len(obj['dices']) >= 3:
            if all(v is not None for v in obj['dices'][:3]):
                _commit_tx(sid, obj['dices'][0], obj['dices'][1], obj['dices'][2], source)
        for v in obj.values():
            if isinstance(v, (dict, list)):
                _extract_tx_from_payload(v, source)
    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, (dict, list)):
                _extract_tx_from_payload(item, source)


_last_joined_sid = None


async def connect_websocket():
    """Kết nối WebSocket (deep-scan + join-room + heartbeat + watchdog) — logic từ sunwin_crawler.py"""
    global current_session_id, _last_joined_sid, _last_message_time

    async def _heartbeat(ws):
        try:
            while True:
                await asyncio.sleep(6)
                if ws.state.name == 'OPEN':
                    await ws.send(json.dumps(TX_HEARTBEAT_MSG))
        except asyncio.CancelledError:
            pass

    async def _watchdog(ws):
        try:
            while True:
                await asyncio.sleep(5)
                if _last_message_time and (time.time() - _last_message_time > 25):
                    print("[🚨 WATCHDOG] 25s im lặng. Force reconnect!")
                    await ws.close()
                    return
        except asyncio.CancelledError:
            pass

    while True:
        try:
            print("[🔄] Đang kết nối WebSocket (tx)...")

            ws_connection = await websockets.connect(
                WEBSOCKET_URL,
                additional_headers=TX_WS_HEADERS,
                open_timeout=10,
                close_timeout=5,
                ping_interval=20,
                ping_timeout=10,
                max_size=2**20
            )

            print("[✅] WebSocket (tx) connected to Sun.Win")

            _last_message_time = time.time()
            for i, msg in enumerate(TX_INITIAL_MESSAGES):
                await ws_connection.send(json.dumps(msg))
                await asyncio.sleep(0.3)

            hb = asyncio.create_task(_heartbeat(ws_connection))
            wd = asyncio.create_task(_watchdog(ws_connection))

            try:
                async for message in ws_connection:
                    _last_message_time = time.time()
                    try:
                        if isinstance(message, bytes):
                            message = message.decode('utf-8', errors='ignore')
                        if not message:
                            continue
                        text = message if isinstance(message, str) else str(message)
                        if text and not text.startswith('[') and not text.startswith('{'):
                            m = re.search(r'[\[{]', text)
                            if m:
                                text = text[m.start():]
                            else:
                                continue
                        data = json.loads(text)

                        _extract_tx_from_payload(data)

                        if not isinstance(data, list) or len(data) < 2 or not isinstance(data[1], dict):
                            continue
                        payload = data[1]
                        sid = payload.get('sid')
                        if sid:
                            current_session_id = sid
                            if sid != _last_joined_sid:
                                join_msg = [6, "MiniGame", "taixiuPlugin", {"cmd": 1007, "sid": sid}]
                                await ws_connection.send(json.dumps(join_msg))
                                _last_joined_sid = sid
                    except json.JSONDecodeError:
                        continue
                    except Exception as e:
                        handle_error("Xử lý message", e)
            finally:
                hb.cancel()
                wd.cancel()

        except websockets.exceptions.ConnectionClosed as e:
            handle_error("WebSocket đóng", e)
            await asyncio.sleep(reconnect_delay)
        except Exception as e:
            handle_error("Kết nối WebSocket", e)
            await asyncio.sleep(reconnect_delay)

# Flask routes
@bp.route('/sunwin/api/tx', methods=['GET'])
def get_tx_result():
    """Endpoint lấy kết quả tài xỉu mới nhất"""
    return jsonify(latest_payload(current_result, build_cau(tx_history)))

@bp.route('/sunwin/api/tx/history', methods=['GET'])
def get_tx_history():
    """Endpoint lấy lịch sử TX"""
    return jsonify({"history": decorate_history(tx_history), "total": len(tx_history)})

@bp.route('/sunwin/api/tx/history/<int:n>', methods=['GET'])
def get_tx_history_n(n):
    """Endpoint lấy n phiên lịch sử TX gần nhất"""
    if n <= 0:
        n = 1
    return jsonify({"history": decorate_history(tx_history, n), "total": len(tx_history)})

@bp.route('/sunwin/api/sicbo', methods=['GET'])
def get_sicbo_result():
    """Endpoint lấy kết quả sicbo mới nhất"""
    return jsonify(latest_payload(sicbo_result, build_cau(sicbo_history)))

@bp.route('/sunwin/api/sicbo/history', methods=['GET'])
def get_sicbo_history():
    """Endpoint lấy lịch sử sicbo"""
    return jsonify({"history": decorate_history(sicbo_history), "total": len(sicbo_history)})

@bp.route('/sunwin/api/sicbo/history/<int:n>', methods=['GET'])
def get_sicbo_history_n(n):
    """Endpoint lấy n phiên lịch sử sicbo gần nhất"""
    if n <= 0:
        n = 1
    return jsonify({"history": decorate_history(sicbo_history, n), "total": len(sicbo_history)})

@bp.route('/sunwin/', methods=['GET'])
def index():
    """Trang chủ"""
    return jsonify({
        "name": "Sun.Win Tài Xỉu Data Stream",
        "version": "1.0",
        "endpoints": {
            "/sunwin/api/tx": "Lấy kết quả tài xỉu mới nhất",
            "/sunwin/api/sicbo": "Lấy kết quả sicbo mới nhất",
            "/sunwin/api/sicbo/history[/n]": "Lấy lịch sử sicbo"
        },
        "thoi_gian": get_vietnam_time(),
        "current_user": TOKEN_DATA.get('username') if TOKEN_DATA else "Unknown"
    })

def start_background():
    """Kích hoạt các worker thread nền (gọi từ app.py router)"""
    global tx_history, sicbo_history
    tx_history = _sload(TX_FILE)
    sicbo_history = _sload("sunwin-sicbo")
    threading.Thread(target=sicbo_poll_loop, daemon=True).start()
    threading.Thread(target=lambda: asyncio.run(connect_websocket()), daemon=True).start()

