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



bp = Blueprint('gamehitclub', __name__)

# Cấu hình logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

POLL_INTERVAL = int(os.getenv('POLL_INTERVAL', 2))
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
    "xuc_xac": [None, None, None],
    "md5_enc": "",
    "md5_dec": ""
}

history100 = []
history101 = []

# Lưu sid phiên chờ kết quả (cmd 1008) cho TX
pending_sid = None
last_sid100 = None
last_sid101 = None

API_BASE_URL = 'https://jakpotgwab.geightdors.net/glms/v1/notify/taixiu?platform_id=g8&gid=vgmn_{}'
HEADERS = {'User-Agent': 'Mozilla/5.0'}

def get_tai_xiu(d1, d2, d3):
    """Kết quả Tài/Xỉu: tổng <= 10 là Xỉu, ngược lại là Tài"""
    return 'Xỉu' if (d1 + d2 + d3) <= 10 else 'Tài'


def update_result(store, history, game_data, is_md5=False):
    """Cập nhật kết quả mới nhất từ dữ liệu game"""
    sid = game_data.get("sid")
    d1 = game_data.get("d1")
    d2 = game_data.get("d2")
    d3 = game_data.get("d3")
    md5 = game_data.get("md5", "")
    rs = game_data.get("rs", "")

    if sid is None or d1 is None or d2 is None or d3 is None:
        logger.warning(f"⚠️ dữ liệu không hợp lệ: sid={sid}, d1={d1}, d2={d2}, d3={d3}")
        return

    total = d1 + d2 + d3
    result = get_tai_xiu(d1, d2, d3)
    last_update = datetime.now().isoformat()

    store.clear()
    store.update({
        "ket_qua": result,
        "last_update": last_update,
        "phien": sid,
        "tong": total,
        "xuc_xac": [d1, d2, d3]
    })

    if is_md5:
        store["md5_enc"] = md5 if md5 else ""
        store["md5_dec"] = rs if rs else ""

    history.insert(0, {
        "phien": sid,
        "xuc_xac": [d1, d2, d3],
        "tong": total,
        "ket_qua": result,
        "timestamp": last_update
    })
    if len(history) > MAX_HISTORY:
        history.pop()

    save('hitclub-md5' if is_md5 else 'hitclub-tx', history)

    tag = '[MD5]' if is_md5 else '[TX]'
    time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    logger.info(f"[🎲✅] {tag} phiên {sid} - {d1}-{d2}-{d3} ➜ tổng: {total}, kết quả: {result} "
                f"| cau={build_cau(history)} | {time_str}")


def fetch_game_data(gid, store, history, is_md5):
    """Lấy dữ liệu từ API nguồn"""
    global pending_sid, last_sid100, last_sid101
    api_url = API_BASE_URL.format(gid)
    try:
        response = requests.get(api_url, headers=HEADERS, timeout=10)
        if response.status_code != 200:
            return
        data = response.json()

        if data.get("status") == "OK" and isinstance(data.get("data"), list):
            # pass 1: capture sid cho TX (chỉ gid không md5)
            if not is_md5:
                for game in data["data"]:
                    if game.get("cmd") == 1008:
                        sid = game.get("sid")
                        if sid:
                            pending_sid = sid

            # pass 2: xử lý kết quả
            for game in data["data"]:
                cmd = game.get("cmd")
                sid = game.get("sid")
                d1 = game.get("d1")
                d2 = game.get("d2")
                d3 = game.get("d3")

                if is_md5 and cmd == 2006:
                    if sid and sid != last_sid101 and d1 is not None and d2 is not None and d3 is not None:
                        last_sid101 = sid
                        update_result(store, history, game, is_md5=True)

                elif not is_md5 and cmd == 1003:
                    active_sid = pending_sid
                    if active_sid and active_sid != last_sid100 and d1 is not None and d2 is not None and d3 is not None:
                        last_sid100 = active_sid
                        game["sid"] = active_sid
                        update_result(store, history, game, is_md5=False)
                        pending_sid = None

    except requests.exceptions.RequestException as e:
        logger.error(f"❌ lỗi khi lấy dữ liệu từ api get {gid}: {e}")
    except Exception as e:
        logger.error(f"❌ lỗi xử lý dữ liệu {gid}: {e}")


def start_fetching():
    """Chạy fetch dữ liệu định kỳ cho cả 2 gid"""
    global history100, history101, sicbo_history
    history100 = load('hitclub-tx')
    history101 = load('hitclub-md5')
    sicbo_history = load('hitclub-sicbo')
    def poll_loop(gid, store, history, is_md5):
        while True:
            try:
                fetch_game_data(gid, store, history, is_md5)
            except Exception as e:
                logger.error(f"❌ lỗi trong poll_loop {gid}: {e}")
            time.sleep(POLL_INTERVAL)

    thread100 = threading.Thread(target=poll_loop, args=('100', latest_result, history100, False), daemon=True)
    thread100.start()
    thread101 = threading.Thread(target=poll_loop, args=('101', latest_result_md5, history101, True), daemon=True)
    thread101.start()

    threading.Thread(target=sicbo_poll_loop, daemon=True).start()


# ==================== SICBO (nguồn: api.wsmt8g.cc/ktrng_3932) ====================
SICBO_BASE_URL = 'https://api.wsmt8g.cc/v2/history/getLastResult'
SICBO_PARAMS = {
    'gameId': 'ktrng_3932',
    'tableId': '39321215743193',
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
        response = requests.get(SICBO_BASE_URL, params=SICBO_PARAMS, headers=HEADERS, timeout=10)
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
    """Chuyển 1 record sicbo thành entry format TX (hitclub)"""
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
                save('hitclub-sicbo', sicbo_history)
                e = latest_sicbo
                logger.info(f"[🎲🧨] SICBO phiên {e['phien']} - "
                            f"{'-'.join(str(x) if x is not None else '?' for x in e['xuc_xac'])} "
                            f"➜ tổng: {e['tong']}, kết quả: {e['ket_qua']} | cau={build_cau(sicbo_history)} | {now}")
        except Exception as e:
            logger.error(f"❌ lỗi trong sicbo_poll_loop: {e}")
        time.sleep(POLL_INTERVAL)


@bp.route("/hitclub/api/tx", methods=['GET'])
def get_tx():
    """Endpoint trả về kết quả TX thường (gid 100)"""
    return jsonify(latest_payload(latest_result, build_cau(history100)))

@bp.route("/hitclub/api/md5", methods=['GET'])
def get_md5():
    """Endpoint trả về kết quả TX MD5 (gid 101)"""
    return jsonify(latest_payload(latest_result_md5, build_cau(history101)))

@bp.route("/hitclub/api/tx/history", methods=['GET'])
def get_tx_history():
    """Endpoint trả về lịch sử TX thường"""
    return jsonify({"history": decorate_history(history100), "total": len(history100)})

@bp.route("/hitclub/api/tx/history/<int:n>", methods=['GET'])
def get_tx_history_n(n):
    """Endpoint lấy n phiên lịch sử TX gần nhất"""
    if n <= 0:
        n = 1
    return jsonify({"history": decorate_history(history100, n), "total": len(history100)})

@bp.route("/hitclub/api/md5/history", methods=['GET'])
def get_md5_history():
    """Endpoint trả về lịch sử TX MD5"""
    return jsonify({"history": decorate_history(history101), "total": len(history101)})

@bp.route("/hitclub/api/md5/history/<int:n>", methods=['GET'])
def get_md5_history_n(n):
    """Endpoint lấy n phiên lịch sử TX MD5 gần nhất"""
    if n <= 0:
        n = 1
    return jsonify({"history": decorate_history(history101, n), "total": len(history101)})

@bp.route("/hitclub/api/sicbo", methods=['GET'])
def get_sicbo():
    """Endpoint trả về kết quả sicbo mới nhất"""
    return jsonify(latest_payload(latest_sicbo, build_cau(sicbo_history)))

@bp.route("/hitclub/api/sicbo/history", methods=['GET'])
def get_sicbo_history():
    """Endpoint trả về lịch sử sicbo"""
    return jsonify({"history": decorate_history(sicbo_history), "total": len(sicbo_history)})

@bp.route("/hitclub/api/sicbo/history/<int:n>", methods=['GET'])
def get_sicbo_history_n(n):
    """Endpoint lấy n phiên lịch sử sicbo gần nhất"""
    if n <= 0:
        n = 1
    return jsonify({"history": decorate_history(sicbo_history, n), "total": len(sicbo_history)})


def start_background():
    start_fetching()
