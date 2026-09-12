import asyncio
import json
import base64
import os
import threading
import logging
import ssl
import urllib.parse
import requests
import websockets
import certifi
from datetime import datetime


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


from flask import Blueprint, jsonify
from flask_cors import CORS

bp = Blueprint('gamesao789', __name__)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

MAX_HISTORY = 100

# Cấu trúc dữ liệu mặc định (định dạng giống 789/789.py)
latest_result = {
    "ket_qua": None,
    "last_update": None,
    "phien": None,
    "tong": None,
    "xuc_xac": [None, None, None]
}

latest_result_md5 = {
    "ket_qua": None,
    "last_update": None,
    "phien": None,
    "tong": None,
    "xuc_xac": [None, None, None]
}

tx_history = []
md5_history = []

last_sid_tx = None
last_sid_md5 = None

# ==================== SAO789 (theo sao789.js) ====================
AUTH_URL = "https://api.api-sao789.space/id/res"
AUTH_HEADERS = {
    "Host": "api.api-sao789.space",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Content-Type": "application/json",
    "Origin": "https://play.sao789a.me",
    "Referer": "https://play.sao789a.me/",
    "mc": "YTJoa2JtbGtiVzA9fC05N3wxNzg1OTI2MTcwMzgwfDU5Y2M3NTFmYWM4MjNjZjM5MWQwZGNiOTdmMGM1MjAwXzg5N2IyYzIxZDVjMmM5YjkzNWYxYTU2ODRhMmJkODc1fGU3OTQyM2JkYmI4ODQyMjgzNDRmMmI4ZjM1Yjg1MGMwfDE5OTY2NDZB"
}
AUTH_PAYLOAD = {
    "command": "login2",
    "deviceId": "250100646453736150000537365900160024",
    "password": "kiet2012",
    "platformId": 4,
    "username": "kietdz2012"
}
WS_BASE = "wss://api.api-sao789.space/websocket?d="
WS_HEADERS = {
    "Host": "api.api-sao789.space",
    "Origin": "https://play.sao789a.me",
    "User-Agent": "Mozilla/5.0"
}

SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

auth_data = None


def get_tai_xiu(d1, d2, d3):
    return 'Tài' if (d1 + d2 + d3) >= 11 else 'Xỉu'


def fetch_auth_data():
    """Lấy thông tin xác thực từ API và trả về URL websocket"""
    global auth_data
    try:
        logger.info("🔄 [Sao789] Đang lấy dữ liệu xác thực từ API...")
        resp = requests.post(AUTH_URL, json=AUTH_PAYLOAD, headers=AUTH_HEADERS,
                             timeout=10, verify=certifi.where())
        res_json = resp.json()
        if res_json.get('status') != 0 or not res_json.get('data'):
            logger.error(f"❌ [Sao789] API Auth Error: {str(res_json)[:200]}")
            return None
        data = res_json['data']
        auth_data = {
            "username": AUTH_PAYLOAD['username'],
            "password": AUTH_PAYLOAD['password'],
            "payload": {
                "signature": data.get('signature'),
                "info": data.get('info'),
                "pid": AUTH_PAYLOAD['platformId']
            }
        }
        access_token = data.get('accessToken')
        refresh_token = (data.get('refreshToken') or '').replace('.', '_')
        timestamp = str(int(round(time_ms())))

        part1 = "a2hkbmlkbW0="
        part2 = "-97"
        part6 = "1996646A"

        raw_d = f"{part1}|{part2}|{timestamp}|{refresh_token}|{access_token}|{part6}"
        d_param = base64.b64encode(raw_d.encode('utf-8')).decode('utf-8')
        d_param_encoded = urllib.parse.quote(d_param, safe='')
        logger.info("✅ [Sao789] Lấy Auth thành công!")
        return WS_BASE + d_param_encoded
    except Exception as e:
        logger.error(f"❌ [Sao789] Lỗi fetch auth: {e}")
        return None


def time_ms():
    import time
    return time.time() * 1000


def add_record(history, store, entry, tag):
    """Thêm/sửa phiên vào history (dedupe theo phien) + cập nhật store"""
    now = datetime.now().isoformat()
    record = dict(entry)
    record["timestamp"] = now

    found = False
    for i, r in enumerate(history):
        if r["phien"] == record["phien"]:
            history[i] = record
            found = True
            break
    if not found:
        history.insert(0, record)
    if len(history) > MAX_HISTORY:
        del history[MAX_HISTORY:]

    if tag == '[MD5]':
        save('sao789-md5', history)
    else:
        save('sao789-tx', history)

    store.clear()
    store.update({
        "ket_qua": record["ket_qua"],
        "last_update": now,
        "phien": record["phien"],
        "tong": record["tong"],
        "xuc_xac": record["xuc_xac"]
    })
    if not found:
        dice_str = '-'.join(str(x) if x is not None else '?' for x in record["xuc_xac"])
        logger.info(f"[🎲✅] {tag} phiên {record['phien']} - {dice_str} "
                    f"➜ tổng: {record['tong']}, kết quả: {record['ket_qua']} | cau={build_cau(history)} | {now}")


def process_htr(htr, store, history, tag, cmd):
    """Lấy phiên mới nhất từ htr (theo logic sao789.js)"""
    global last_sid_tx, last_sid_md5
    if not isinstance(htr, list) or not htr:
        return
    latest = max(htr, key=lambda x: x.get('sid') or 0)
    sid = latest.get('sid')
    if not sid:
        return
    last = last_sid_tx if cmd == 1005 else last_sid_md5
    if sid == last:
        return
    if cmd == 1005:
        last_sid_tx = sid
    else:
        last_sid_md5 = sid
    d1 = latest.get('d1')
    d2 = latest.get('d2')
    d3 = latest.get('d3')
    if d1 is None or d2 is None or d3 is None:
        return
    add_record(history, store, {
        "phien": str(sid),
        "xuc_xac": [d1, d2, d3],
        "tong": d1 + d2 + d3,
        "ket_qua": get_tai_xiu(d1, d2, d3)
    }, tag)


def process_message(message):
    try:
        raw = message.decode('utf-8') if isinstance(message, bytes) else message
        parsed = json.loads(raw)
        if isinstance(parsed, list) and len(parsed) >= 2 and isinstance(parsed[1], dict):
            cmd = parsed[1].get('cmd')
            if cmd == 1005:
                process_htr(parsed[1].get('htr'), latest_result, tx_history, '[TX]', 1005)
            elif cmd == 1105:
                process_htr(parsed[1].get('htr'), latest_result_md5, md5_history, '[MD5]', 1105)
    except Exception:
        pass


async def send_commands(ws):
    """Gửi lệnh khởi tạo plugin TX & MD5 sau 2s"""
    await asyncio.sleep(2)
    try:
        await ws.send(json.dumps([6, "MiniGame", "taixiuPlugin", {"cmd": 1005}]))
        await ws.send(json.dumps([6, "MiniGame", "taixiuMd5Plugin", {"cmd": 1105}]))
        await ws.send(json.dumps([6, "MiniGame", "lobbyPlugin", {"cmd": 10001}]))
        logger.info("📤 [Sao789] Đã gửi lệnh khởi tạo plugin TX & MD5")
    except Exception as e:
        logger.error(f"❌ [Sao789] Gửi lệnh khởi tạo lỗi: {e}")


async def heartbeat(ws):
    counter = 0
    while True:
        await asyncio.sleep(25)
        try:
            await ws.send(json.dumps([7, "MiniGame", counter, int(time_ms())]))
            counter += 1
        except Exception:
            break


async def refresh(ws):
    """Gửi lệnh refresh data mỗi 30s"""
    while True:
        await asyncio.sleep(30)
        try:
            await ws.send(json.dumps([6, "MiniGame", "taixiuPlugin", {"cmd": 1005}]))
            await ws.send(json.dumps([6, "MiniGame", "taixiuMd5Plugin", {"cmd": 1105}]))
        except Exception:
            break


async def ws_loop():
    global auth_data
    while True:
        try:
            ws_url = fetch_auth_data()
            if not ws_url:
                logger.info("⏳ Thử lấy lại Auth sau 5 giây...")
                await asyncio.sleep(5)
                continue

            logger.info("🔗 [Sao789] Đang kết nối WebSocket...")
            async with websockets.connect(
                ws_url,
                additional_headers=WS_HEADERS,
                ssl=SSL_CTX,
                ping_interval=15,
                ping_timeout=10
            ) as conn:
                logger.info("✅ [Sao789] WebSocket đã kết nối!")
                await conn.send(json.dumps([
                    1, "MiniGame", auth_data["username"], auth_data["password"], auth_data["payload"]
                ]))
                tasks = [
                    asyncio.create_task(send_commands(conn)),
                    asyncio.create_task(heartbeat(conn)),
                    asyncio.create_task(refresh(conn))
                ]
                async for message in conn:
                    try:
                        process_message(message)
                    except Exception as e:
                        logger.error(f"⚠️ Lỗi xử lý message Sao789: {e}")
                for t in tasks:
                    t.cancel()
        except Exception as e:
            logger.error(f"🔥 [Sao789] WS crash: {e}")
        await asyncio.sleep(5)


# ==================== API ROUTES ====================
@bp.route('/sao789/api/tx', methods=['GET'])
def get_tx():
    """Kết quả TX mới nhất"""
    return jsonify(latest_payload(latest_result, build_cau(tx_history)))

@bp.route('/sao789/api/md5', methods=['GET'])
def get_md5():
    """Kết quả MD5 mới nhất"""
    return jsonify(latest_payload(latest_result_md5, build_cau(md5_history)))

@bp.route('/sao789/api/tx/history', methods=['GET'])
def get_tx_history():
    """Lịch sử TX"""
    return jsonify({"history": decorate_history(tx_history), "total": len(tx_history)})

@bp.route('/sao789/api/tx/history/<int:n>', methods=['GET'])
def get_tx_history_n(n):
    """Lịch sử n phiên TX gần nhất"""
    if n <= 0:
        n = 1
    return jsonify({"history": decorate_history(tx_history, n), "total": len(tx_history)})

@bp.route('/sao789/api/md5/history', methods=['GET'])
def get_md5_history():
    """Lịch sử MD5"""
    return jsonify({"history": decorate_history(md5_history), "total": len(md5_history)})

@bp.route('/sao789/api/md5/history/<int:n>', methods=['GET'])
def get_md5_history_n(n):
    """Lịch sử n phiên MD5 gần nhất"""
    if n <= 0:
        n = 1
    return jsonify({"history": decorate_history(md5_history, n), "total": len(md5_history)})

@bp.route('/sao789/', methods=['GET'])
def index():
    return jsonify({
        "service": "Sao789 Tài Xỉu (TX + MD5)",
        "endpoints": {
            "/sao789/api/tx": "Kết quả TX mới nhất",
            "/sao789/api/md5": "Kết quả MD5 mới nhất",
            "/sao789/api/tx/history[/n]": "Lịch sử TX",
            "/sao789/api/md5/history[/n]": "Lịch sử MD5"
        },
        "thoi_gian": datetime.now().strftime("%d-%m-%Y %H:%M:%S UTC+7")
    })

def start_background():
    """Kích hoạt worker nền (gọi từ app.py router)"""
    global tx_history, md5_history
    tx_history = load('sao789-tx')
    md5_history = load('sao789-md5')
    def _run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.create_task(ws_loop())
        loop.run_forever()
    threading.Thread(target=_run, daemon=True).start()

