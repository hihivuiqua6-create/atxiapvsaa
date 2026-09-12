from flask import Blueprint, jsonify
from flask_cors import CORS
import requests
import os
import threading
import time
import logging
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



bp = Blueprint('gamerikvip', __name__)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

POLL_INTERVAL = 5
MAX_HISTORY = 100

# Nguồn dữ liệu (theo rikvip.js): jakpotgwab - 2 bàn HU (vgmn_100) và MD5 (vgmn_101)
API_BASE = 'https://jakpotgwab.geightdors.net/glms/v1/notify/taixiu'
API_GIDS = {
    'hu': 'vgmn_100',
    'md5': 'vgmn_101'
}
API_HEADERS = {'User-Agent': 'Mozilla/5.0'}

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

hu_history = []
md5_history = []

last_sid = {
    'hu': None,
    'md5': None
}
pending_sid_hu = None


def get_vietnam_time():
    """Thời gian UTC+7"""
    now = datetime.now()
    return now.strftime("%d-%m-%Y %H:%M:%S UTC+7")


def tinh_tai_xiu(d1, d2, d3):
    return 'Xỉu' if (d1 + d2 + d3) <= 10 else 'Tài'


def to_entry(phien, d1, d2, d3):
    return {
        "phien": phien,
        "xuc_xac": [d1, d2, d3],
        "tong": d1 + d2 + d3,
        "ket_qua": tinh_tai_xiu(d1, d2, d3)
    }


def update_store(store, entry, tag, history=None):
    """Cập nhật kết quả mới nhất"""
    store.clear()
    store.update({
        "ket_qua": entry["ket_qua"],
        "last_update": datetime.now().isoformat(),
        "phien": entry["phien"],
        "tong": entry["tong"],
        "xuc_xac": entry["xuc_xac"]
    })
    dice_str = '-'.join(str(x) for x in store['xuc_xac'])
    logger.info(f"[🎲✅] {tag} phiên {store['phien']} - {dice_str} "
                f"➜ tổng: {store['tong']}, kết quả: {store['ket_qua']} | cau={build_cau(history or [])} | {get_vietnam_time()}")


def add_record(history, store, entry, tag):
    """Thêm phiên mới vào history + cập nhật store"""
    record = dict(entry)
    record["timestamp"] = datetime.now().isoformat()
    history.insert(0, record)
    if len(history) > MAX_HISTORY:
        del history[MAX_HISTORY:]
    update_store(store, entry, tag, history)
    if tag == '[MD5]':
        save('rikvip-md5', history)
    else:
        save('rikvip-tx', history)


def fetch(gid):
    """Lấy dữ liệu từ API jakpotgwab"""
    try:
        response = requests.get(
            API_BASE,
            params={'platform_id': 'rik', 'gid': API_GIDS[gid]},
            headers=API_HEADERS,
            timeout=10
        )
        response.raise_for_status()
        data = response.json()
        if data.get('status') != 'OK':
            logger.warning(f"⚠️ [{gid}] API trả status: {data.get('status')}")
            return None
        messages = data.get('data')
        if not isinstance(messages, list):
            return None
        return messages
    except requests.exceptions.RequestException as e:
        logger.error(f"❌ Lỗi fetch dữ liệu {gid}: {e}")
    except Exception as e:
        logger.error(f"❌ Lỗi xử lý dữ liệu {gid}: {e}")
    return None


def process_hu(messages):
    """Xử lý bàn HU: cmd 1008 báo phiên, cmd 1003 trả kết quả (không kèm sid)
    → giữ pending_sid_hu xuyên các poll; 1003 xử lý trước, 1008 cập nhật sau"""
    global last_sid, pending_sid_hu

    for msg in messages:
        if not isinstance(msg, dict):
            continue
        try:
            cmd = int(msg.get('cmd'))
        except (TypeError, ValueError):
            continue
        if cmd == 1003:
            d1 = msg.get('d1')
            d2 = msg.get('d2')
            d3 = msg.get('d3')
            if pending_sid_hu and pending_sid_hu != last_sid['hu'] and d1 is not None and d2 is not None and d3 is not None:
                last_sid['hu'] = pending_sid_hu
                add_record(hu_history, latest_result, to_entry(pending_sid_hu, d1, d2, d3), '[TX/HU]')
                pending_sid_hu = None

    for msg in messages:
        if not isinstance(msg, dict):
            continue
        try:
            cmd = int(msg.get('cmd'))
        except (TypeError, ValueError):
            continue
        if cmd == 1008 and msg.get('sid'):
            pending_sid_hu = msg.get('sid')


def process_md5(messages):
    """Xử lý bàn MD5 (theo logic rikvip.js): cmd 2006 / 7006 trả kết quả kèm phiên"""
    global last_sid
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        try:
            cmd = int(msg.get('cmd'))
        except (TypeError, ValueError):
            continue
        if cmd in (2006, 7006):
            sid = msg.get('sid')
            d1 = msg.get('d1')
            d2 = msg.get('d2')
            d3 = msg.get('d3')
            if sid and sid != last_sid['md5'] and d1 is not None and d2 is not None and d3 is not None:
                last_sid['md5'] = sid
                add_record(md5_history, latest_result_md5, to_entry(sid, d1, d2, d3), '[MD5]')


def poll_loop(gid, processor):
    """Poll định kỳ nguồn dữ liệu"""
    while True:
        try:
            messages = fetch(gid)
            if messages:
                processor(messages)
        except Exception as e:
            logger.error(f"❌ Lỗi trong poll_loop {gid}: {e}")
        time.sleep(POLL_INTERVAL)


def start_polling():
    """Chạy poll cho cả 2 bàn HU và MD5"""
    global hu_history, md5_history, sicbo_history
    hu_history = load('rikvip-tx')
    md5_history = load('rikvip-md5')
    sicbo_history = load('rikvip-sicbo')
    threading.Thread(target=poll_loop, args=('hu', process_hu), daemon=True).start()
    threading.Thread(target=poll_loop, args=('md5', process_md5), daemon=True).start()
    threading.Thread(target=sicbo_poll_loop, daemon=True).start()


# ==================== SICBO (nguồn: api.wsmt8g.cc/ktrng_3980) ====================
SICBO_BASE_URL = 'https://api.wsmt8g.cc/v2/history/getLastResult'
SICBO_PARAMS = {
    'gameId': 'ktrng_3980',
    'tableId': '39801215743193',
    'size': 100,
    'curPage': 1
}

latest_sicbo = {
    "ket_qua": None,
    "last_update": None,
    "phien": None,
    "tong": None,
    "xuc_xac": [None, None, None]
}
sicbo_history = []

def fetch_sicbo():
    """Lấy lịch sử sicbo từ API (mới nhất trước)"""
    try:
        response = requests.get(SICBO_BASE_URL, params=SICBO_PARAMS, headers=API_HEADERS, timeout=10)
        response.raise_for_status()
        data = response.json()
        result_list = []
        if isinstance(data, dict):
            d = data.get('data')
            if isinstance(d, dict):
                result_list = d.get('resultList', [])
            elif isinstance(d, list):
                result_list = d
        return result_list[:100] if isinstance(result_list, list) else []
    except Exception as e:
        logger.error(f"❌ lỗi fetch sicbo: {e}")
        return None

def to_sicbo_entry(item):
    """Chuyển 1 record sicbo thành entry format TX (rikvip)"""
    faces = item.get('facesList') if isinstance(item.get('facesList'), list) else []
    score = item.get('score') or 0
    faces = (list(faces[:3]) + [None] * 3)[:3]
    return {
        "phien": item.get('gameNum', ''),
        "xuc_xac": faces,
        "tong": score,
        "ket_qua": 'Tài' if score >= 11 else 'Xỉu'
    }

def sicbo_poll_loop():
    """Poll sicbo định kỳ"""
    while True:
        try:
            result_list = fetch_sicbo()
            if result_list:
                now = datetime.now().isoformat()
                latest_sicbo.clear()
                latest_sicbo.update(to_sicbo_entry(result_list[0]))
                latest_sicbo["last_update"] = now
                sicbo_history.clear()
                for r in result_list:
                    entry = to_sicbo_entry(r)
                    entry["timestamp"] = now
                    sicbo_history.append(entry)
                save('rikvip-sicbo', sicbo_history)
                e = latest_sicbo
                logger.info(f"[🎲🧨] SICBO phiên {e['phien']} - "
                            f"{'-'.join(str(x) if x is not None else '?' for x in e['xuc_xac'])} "
                            f"➜ tổng: {e['tong']}, kết quả: {e['ket_qua']} | cau={build_cau(sicbo_history)} | {now}")
        except Exception as e:
            logger.error(f"❌ lỗi trong sicbo_poll_loop: {e}")
        time.sleep(POLL_INTERVAL)


@bp.route('/rikvip/api/tx', methods=['GET'])
def get_tx():
    """Kết quả TX mới nhất"""
    return jsonify(latest_payload(latest_result, build_cau(hu_history)))

@bp.route('/rikvip/api/md5', methods=['GET'])
def get_md5():
    """Kết quả MD5 mới nhất"""
    return jsonify(latest_payload(latest_result_md5, build_cau(md5_history)))

@bp.route('/rikvip/api/tx/history', methods=['GET'])
def get_tx_history():
    """Lịch sử TX"""
    return jsonify({"history": decorate_history(hu_history), "total": len(hu_history)})

@bp.route('/rikvip/api/tx/history/<int:n>', methods=['GET'])
def get_tx_history_n(n):
    """Lịch sử n phiên TX gần nhất"""
    if n <= 0:
        n = 1
    return jsonify({"history": decorate_history(hu_history, n), "total": len(hu_history)})

@bp.route('/rikvip/api/md5/history', methods=['GET'])
def get_md5_history():
    """Lịch sử MD5"""
    return jsonify({"history": decorate_history(md5_history), "total": len(md5_history)})

@bp.route('/rikvip/api/md5/history/<int:n>', methods=['GET'])
def get_md5_history_n(n):
    """Lịch sử n phiên MD5 gần nhất"""
    if n <= 0:
        n = 1
    return jsonify({"history": decorate_history(md5_history, n), "total": len(md5_history)})

@bp.route('/rikvip/api/sicbo', methods=['GET'])
def get_sicbo():
    """Kết quả Sicbo mới nhất"""
    return jsonify(latest_payload(latest_sicbo, build_cau(sicbo_history)))

@bp.route('/rikvip/api/sicbo/history', methods=['GET'])
def get_sicbo_history():
    """Lịch sử Sicbo"""
    return jsonify({"history": decorate_history(sicbo_history), "total": len(sicbo_history)})

@bp.route('/rikvip/api/sicbo/history/<int:n>', methods=['GET'])
def get_sicbo_history_n(n):
    """Lịch sử n phiên Sicbo gần nhất"""
    if n <= 0:
        n = 1
    return jsonify({"history": decorate_history(sicbo_history, n), "total": len(sicbo_history)})

@bp.route('/rikvip/', methods=['GET'])
def index():
    return jsonify({
        "service": "Rikvip Tài Xỉu (HU + MD5)",
        "endpoints": {
            "/rikvip/api/tx": "Kết quả HU mới nhất",
            "/rikvip/api/md5": "Kết quả MD5 mới nhất",
            "/rikvip/api/sicbo": "Kết quả sicbo mới nhất",
            "/rikvip/api/tx/history[/n]": "Lịch sử HU",
            "/rikvip/api/md5/history[/n]": "Lịch sử MD5",
            "/rikvip/api/sicbo/history[/n]": "Lịch sử sicbo"
        },
        "thoi_gian": get_vietnam_time()
    })


def start_background():
    start_polling()
