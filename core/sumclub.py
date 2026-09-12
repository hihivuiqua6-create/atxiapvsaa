import asyncio
import json
import os
import threading
import logging
import urllib.parse
import requests
import websockets
import certifi
from datetime import datetime
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



bp = Blueprint('gamesumclub', __name__)

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

# ==================== SUMCLUB SIGNALR (theo sumclub.js) ====================
SUMCLUB_SERVERS = {
    'TX': {'base': 'https://taixiu.apisum.pro', 'hub': 'luckydiceHub', 'tid': 1},
    'MD5': {'base': 'https://taixiu1.apisum.pro', 'hub': 'luckydice1Hub', 'tid': 2}
}
SUMCLUB_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Origin": "https://play.sum.vin"
}


def get_connection_token(base):
    """Đàm phán SignalR để lấy ConnectionToken"""
    try:
        url = base + "/signalr/negotiate?clientProtocol=1.5"
        res = requests.get(url, headers=SUMCLUB_HEADERS, timeout=5, verify=certifi.where()).json()
        return res.get("ConnectionToken")
    except Exception as e:
        logger.error(f"[Sumclub] Lỗi đàm phán SignalR {base}: {e}")
        return None


def build_ws_url(base, hub, tid):
    token = get_connection_token(base)
    if not token:
        return None
    token = urllib.parse.quote(token)
    connection_data = urllib.parse.quote(json.dumps([{"name": hub}]))
    return (
        base.replace("https", "wss")
        + "/signalr/connect?"
        + "transport=webSockets"
        + f"&connectionToken={token}"
        + f"&connectionData={connection_data}"
        + "&clientProtocol=1.5"
        + f"&tid={tid}"
    )


def get_tai_xiu(d1, d2, d3):
    return 'Tài' if (d1 + d2 + d3) >= 11 else 'Xỉu'


def add_record(history, store, entry, tag):
    """Thêm/sửa phiên vào history (dedupe theo phien) + cập nhật store"""
    now = datetime.now().isoformat()
    record = dict(entry)
    record["phien"] = str(record["phien"])
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
        save('sumclub-md5', history)
    else:
        save('sumclub-tx', history)

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


def parse_sumclub_message(message, store, history, tag):
    """Parse message SignalR (theo sumclub.js): sessionInfo -> Result.Dice1..3"""
    try:
        raw = message.decode('utf-8') if isinstance(message, bytes) else message
        if raw in ("{}", '{"type":6}'):
            return
        data = json.loads(raw)
    except Exception:
        return
    if not isinstance(data, dict) or "M" not in data:
        return
    for m in data["M"]:
        if not isinstance(m, dict):
            continue
        if m.get("M") != "sessionInfo":
            continue
        args = m.get("A") or []
        if not args:
            continue
        obj = args[0]
        if not isinstance(obj, dict):
            continue
        result = obj.get("Result")
        if not isinstance(result, dict):
            continue
        d1 = result.get("Dice1")
        # Nếu xúc xắc 1 == -1 (đang lật bát) thì bỏ qua
        if d1 == -1:
            continue
        session = obj.get("SessionID")
        if not session:
            continue
        d2 = result.get("Dice2")
        d3 = result.get("Dice3")
        if d2 is None or d3 is None:
            continue
        total = d1 + d2 + d3
        entry = {
            "phien": str(session),
            "xuc_xac": [d1, d2, d3],
            "tong": total,
            "ket_qua": get_tai_xiu(d1, d2, d3)
        }
        add_record(history, store, entry, tag)


async def ping_loop(ws, hub):
    """Ping giữ kết nối (PingPong + type:6 mỗi 5s)"""
    i = 1
    while True:
        try:
            await ws.send(json.dumps({"M": "PingPong", "H": hub, "I": i}))
            await ws.send('{"type":6}')
            i += 1
        except Exception:
            break
        await asyncio.sleep(5)


async def ws_client_loop(gtype):
    """Kết nối WebSocket SignalR cho 1 bàn (TX/MD5)"""
    cfg = SUMCLUB_SERVERS[gtype]
    store = latest_result if gtype == 'TX' else latest_result_md5
    history = tx_history if gtype == 'TX' else md5_history
    tag = '[TX]' if gtype == 'TX' else '[MD5]'

    while True:
        try:
            ws_url = build_ws_url(cfg['base'], cfg['hub'], cfg['tid'])
            if not ws_url:
                await asyncio.sleep(2)
                continue
            logger.info(f"[🔄 WS] Đang kết nối Sumclub {gtype}...")
            async with websockets.connect(
                ws_url,
                additional_headers=SUMCLUB_HEADERS,
                ping_interval=15,
                ping_timeout=10
            ) as conn:
                logger.info(f"[✅ WS] Sumclub {gtype} kết nối thành công!")
                ping_task = asyncio.create_task(ping_loop(conn, cfg['hub']))
                async for message in conn:
                    try:
                        parse_sumclub_message(message, store, history, tag)
                    except Exception as e:
                        logger.error(f"[⚠️] Lỗi xử lý message {gtype}: {e}")
        except Exception as e:
            logger.error(f"[🔥 WS CRASH] Sumclub {gtype}: {e}")
        await asyncio.sleep(2)


# ==================== API ROUTES ====================
@bp.route('/sumclub/api/tx', methods=['GET'])
def get_tx():
    """Kết quả TX mới nhất"""
    return jsonify(latest_payload(latest_result, build_cau(tx_history)))

@bp.route('/sumclub/api/md5', methods=['GET'])
def get_md5():
    """Kết quả MD5 mới nhất"""
    return jsonify(latest_payload(latest_result_md5, build_cau(md5_history)))

@bp.route('/sumclub/api/tx/history', methods=['GET'])
def get_tx_history():
    """Lịch sử TX"""
    return jsonify({"history": decorate_history(tx_history), "total": len(tx_history)})

@bp.route('/sumclub/api/tx/history/<int:n>', methods=['GET'])
def get_tx_history_n(n):
    """Lịch sử n phiên TX gần nhất"""
    if n <= 0:
        n = 1
    return jsonify({"history": decorate_history(tx_history, n), "total": len(tx_history)})

@bp.route('/sumclub/api/md5/history', methods=['GET'])
def get_md5_history():
    """Lịch sử MD5"""
    return jsonify({"history": decorate_history(md5_history), "total": len(md5_history)})

@bp.route('/sumclub/api/md5/history/<int:n>', methods=['GET'])
def get_md5_history_n(n):
    """Lịch sử n phiên MD5 gần nhất"""
    if n <= 0:
        n = 1
    return jsonify({"history": decorate_history(md5_history, n), "total": len(md5_history)})

@bp.route('/sumclub/', methods=['GET'])
def index():
    return jsonify({
        "service": "Sumclub Tài Xỉu (TX + MD5) - SignalR Engine",
        "endpoints": {
            "/sumclub/api/tx": "Kết quả TX mới nhất",
            "/sumclub/api/md5": "Kết quả MD5 mới nhất",
            "/sumclub/api/tx/history[/n]": "Lịch sử TX",
            "/sumclub/api/md5/history[/n]": "Lịch sử MD5"
        },
        "thoi_gian": datetime.now().strftime("%d-%m-%Y %H:%M:%S UTC+7")
    })

def start_background():
    """Kích hoạt worker nền (gọi từ app.py router)"""
    global tx_history, md5_history
    tx_history = load('sumclub-tx')
    md5_history = load('sumclub-md5')
    def _run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.create_task(ws_client_loop('TX'))
        loop.create_task(ws_client_loop('MD5'))
        loop.run_forever()
    threading.Thread(target=_run, daemon=True).start()

