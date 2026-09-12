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



bp = Blueprint('gamexocdia88', __name__)

# Cấu hình logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

POLL_INTERVAL = 5
MAX_HISTORY = 100

# Nguồn dữ liệu xocdia88
API_URLS = {
    'tx': 'https://taixiu.system32-cloudfare-356783752985678522.monster/api/luckydice/GetSoiCau',
    'md5': 'https://taixiumd5.system32-cloudfare-356783752985678522.monster/api/md5luckydice/GetSoiCau'
}

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

last_session = {
    'tx': None,
    'md5': None
}


def parse_lines(data):
    """Parse danh sách phiên từ API"""
    if not isinstance(data, list):
        return []
    items = sorted(data, key=lambda x: x.get("SessionId") or 0)
    return [{
        "session": item.get("SessionId"),
        "dice": [item.get("FirstDice"), item.get("SecondDice"), item.get("ThirdDice")],
        "total": item.get("DiceSum"),
        "result": _result_from_item(item)
    } for item in items]


def _result_from_item(item):
    """BetSide: 0=TAI, 1=XIU, còn lại dựa vào tổng (>=11 là TAI)"""
    bet_side = item.get("BetSide")
    if bet_side == 0:
        return "TAI"
    if bet_side == 1:
        return "XIU"
    return "TAI" if (item.get("DiceSum") or 0) >= 11 else "XIU"


def to_entry(record):
    """Chuyển 1 record phiên thành entry format 789"""
    dice = record["dice"]
    total = record["total"] or sum(x or 0 for x in dice)
    return {
        "phien": record["session"],
        "xuc_xac": list(dice[:3]) + [None] * (3 - len(dice[:3])),
        "tong": total,
        "ket_qua": 'Tài' if record["result"] == 'TAI' else 'Xỉu'
    }


def update_store(store, entry, tag, history=None):
    """Cập nhật store kết quả mới nhất (không đụng history)"""
    last_update = datetime.now().isoformat()
    store.clear()
    store.update({
        "ket_qua": entry["ket_qua"],
        "last_update": last_update,
        "phien": entry["phien"],
        "tong": entry["tong"],
        "xuc_xac": entry["xuc_xac"]
    })
    time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    dice_str = '-'.join(str(x) if x is not None else '?' for x in store['xuc_xac'])
    logger.info(f"[🎲✅] {tag} phiên {store['phien']} - {dice_str} ➜ tổng: {store['tong']}, "
                f"kết quả: {store['ket_qua']} | cau={build_cau(history or [])} | {time_str}")


def fetch_and_process(gid, store, history, file_key):
    """Lấy dữ liệu và xử lý phiên mới"""
    global last_session
    try:
        response = requests.get(API_URLS[gid], timeout=10)
        response.raise_for_status()
        data = response.json()
        records = parse_lines(data)

        if not records:
            logger.warning("⚠️ Không có dữ liệu từ API.")
            return

        latest = records[-1]
        current = last_session[gid]
        tag = '[MD5]' if gid == 'md5' else '[TX]'

        if current is None:
            # Lần đầu: nạp toàn bộ lịch sử hiện tại
            for r in records[-MAX_HISTORY:]:
                entry = to_entry(r)
                entry["timestamp"] = datetime.now().isoformat()
                history.append(entry)
            history.reverse()
            last_session[gid] = latest["session"]
            update_store(store, to_entry(latest), tag, history)
            logger.info(f"✅ Đã tải {len(records)} phiên lịch sử {gid.title()}.")
        elif latest["session"] > current:
            new_records = [r for r in records if r["session"] > current]
            for r in new_records:
                entry = to_entry(r)
                entry["timestamp"] = datetime.now().isoformat()
                history.insert(0, entry)
            if len(history) > MAX_HISTORY:
                del history[MAX_HISTORY:]
            last_session[gid] = latest["session"]
            update_store(store, to_entry(latest), tag, history)
            if new_records:
                logger.info(f"🆕 Cập nhật {len(new_records)} phiên {gid.title()}. Phiên cuối: {current}")

        save(file_key, history)

    except requests.exceptions.RequestException as e:
        logger.error(f"❌ Lỗi fetch dữ liệu {gid}: {e}")
    except Exception as e:
        logger.error(f"❌ Lỗi xử lý dữ liệu {gid}: {e}")


def start_fetching():
    """Chạy fetch định kỳ cho cả 2 nguồn tx và md5"""
    global tx_history, md5_history
    tx_history = load('xocdia88-tx')
    md5_history = load('xocdia88-md5')
    def poll_loop(gid, store, history, file_key):
        while True:
            try:
                fetch_and_process(gid, store, history, file_key)
            except Exception as e:
                logger.error(f"❌ Lỗi trong poll_loop {gid}: {e}")
            time.sleep(POLL_INTERVAL)

    threading.Thread(target=poll_loop, args=('tx', latest_result, tx_history, 'xocdia88-tx'), daemon=True).start()
    threading.Thread(target=poll_loop, args=('md5', latest_result_md5, md5_history, 'xocdia88-md5'), daemon=True).start()


@bp.route("/xocdia88/api/tx", methods=['GET'])
def get_tx():
    """Endpoint trả về kết quả TX mới nhất"""
    return jsonify(latest_payload(latest_result, build_cau(tx_history)))

@bp.route("/xocdia88/api/md5", methods=['GET'])
def get_md5():
    """Endpoint trả về kết quả TX MD5 mới nhất"""
    return jsonify(latest_payload(latest_result_md5, build_cau(md5_history)))

@bp.route("/xocdia88/api/tx/history", methods=['GET'])
def get_tx_history():
    """Endpoint trả về lịch sử TX"""
    return jsonify({"history": decorate_history(tx_history), "total": len(tx_history)})

@bp.route("/xocdia88/api/tx/history/<int:n>", methods=['GET'])
def get_tx_history_n(n):
    """Endpoint lấy n phiên lịch sử TX gần nhất"""
    if n <= 0:
        n = 1
    return jsonify({"history": decorate_history(tx_history, n), "total": len(tx_history)})

@bp.route("/xocdia88/api/md5/history", methods=['GET'])
def get_md5_history():
    """Endpoint trả về lịch sử TX MD5"""
    return jsonify({"history": decorate_history(md5_history), "total": len(md5_history)})

@bp.route("/xocdia88/api/md5/history/<int:n>", methods=['GET'])
def get_md5_history_n(n):
    """Endpoint lấy n phiên lịch sử TX MD5 gần nhất"""
    if n <= 0:
        n = 1
    return jsonify({"history": decorate_history(md5_history, n), "total": len(md5_history)})


def start_background():
    start_fetching()
