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



bp = Blueprint('gameluck8', __name__)

# Cấu hình logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

POLL_INTERVAL = 5
MAX_HISTORY = 100

API_BASE = "https://luck8bot.com/api/GetNewLottery/"

# Các game tài xỉu / sicbo (xúc xắc)
DICE_GAMES = {
    "Taixiu", "TaixiuMd3", "TaixiuMd5",
    "Sicbo20", "Sicbo30", "Sicbo40", "Sicbo50", "Sicbo1m", "Sicbo1_5m"
}

# Các game keno
KENO_GAMES = {
    "KenoVip20", "KenoVip30", "KenoVip40", "KenoVip50", "KenoVip1m", "KenoVip5m"
}

# Các game xổ số (phần còn lại)
GAMES = [
    "Taixiu", "TaixiuMd3", "TaixiuMd5",
    "MienBacMoi5m", "MienBacMoi3m", "MienBac", "MienBacVip45", "MienBacVip75", "MienBacVip2m",
    "MienNamVip45", "MienNamVip90", "MienNamVip1m", "MienNamVip2m", "MienNamVip5m",
    "KenoVip20", "KenoVip30", "KenoVip40", "KenoVip50", "KenoVip1m", "KenoVip5m",
    "Sicbo20", "Sicbo30", "Sicbo40", "Sicbo50", "Sicbo1m", "Sicbo1_5m"
]


def game_type(gid):
    """Xác định loại game: dice / keno / xoso"""
    if gid in DICE_GAMES:
        return 'dice'
    if gid in KENO_GAMES:
        return 'keno'
    return 'xoso'


lock = threading.Lock()
latest = {}
history = {}
last_id = {}

for g in GAMES:
    latest[g] = {}
    history[g] = []
    last_id[g] = None


def parse_numbers(raw):
    """Tách OpenCode thành danh sách số nguyên"""
    open_code = raw.get("OpenCode", "")
    return [int(x) for x in open_code.split(",") if x.strip() != ""]


def build_entry(gid, raw):
    """Chuyển 1 phiên thành entry format chuẩn theo loại game"""
    numbers = parse_numbers(raw)
    phien = raw.get("Expect", "")
    timestamp = datetime.now().isoformat()

    if game_type(gid) == 'dice':
        dice = (numbers + [0, 0, 0])[:3]
        tong = sum(dice)
        if 11 <= tong <= 17:
            ket_qua = "Tài"
        elif 4 <= tong <= 10:
            ket_qua = "Xỉu"
        else:
            ket_qua = "Không xác định"
        return {
            "phien": phien,
            "xuc_xac": dice,
            "tong": tong,
            "ket_qua": ket_qua,
            "last_update": timestamp
        }

    if game_type(gid) == 'keno':
        return {
            "phien": phien,
            "so": numbers,
            "tong": sum(numbers) if numbers else None,
            "timestamp": timestamp
        }

    # Xổ số
    return {
        "phien": phien,
        "ket_qua": ''.join(str(n) for n in numbers),
        "timestamp": timestamp
    }


def update_store(gid, entry):
    """Cập nhật kết quả mới nhất (không đụng history)"""
    store = latest[gid]
    store.clear()
    store.update(entry)

    time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if game_type(gid) == 'dice':
        dice_str = '-'.join(str(x) for x in store['xuc_xac'])
        logger.info(f"[🎲✅] {gid} phiên {store['phien']} - {dice_str} ➜ tổng: {store['tong']}, "
                    f"kết quả: {store['ket_qua']} | cau={build_cau(history.get(gid, []))} | {time_str}")
    elif game_type(gid) == 'keno':
        logger.info(f"[🎲✅] {gid} phiên {store['phien']} - {len(store['so'])} số ➜ tổng: {store['tong']} | {time_str}")
    else:
        logger.info(f"[🎲✅] {gid} phiên {store['phien']} - kết quả: {store['ket_qua']} | {time_str}")


def fetch_and_process(gid):
    """Lấy dữ liệu và xử lý phiên mới"""
    global last_id
    try:
        response = requests.get(API_BASE + gid, timeout=10)
        response.raise_for_status()
        data = response.json()

        if data.get("state") != 1 or not data.get("data"):
            return

        raw = data["data"]
        sid = raw.get("ID")
        if sid is None:
            return

        current = last_id[gid]

        with lock:
            if current is None:
                entry = build_entry(gid, raw)
                history[gid].insert(0, entry)
                last_id[gid] = sid
                update_store(gid, entry)
                logger.info(f"✅ Đã tải 1 phiên lịch sử {gid}.")
            elif sid > current:
                entry = build_entry(gid, raw)
                history[gid].insert(0, entry)
                if len(history[gid]) > MAX_HISTORY:
                    del history[gid][MAX_HISTORY:]
                last_id[gid] = sid
                update_store(gid, entry)

            save(f"luck8-{gid.lower()}", history[gid])

    except requests.exceptions.RequestException as e:
        logger.error(f"❌ Lỗi fetch dữ liệu {gid}: {e}")
    except Exception as e:
        logger.error(f"❌ Lỗi xử lý dữ liệu {gid}: {e}")


def start_fetching():
    """Chạy fetch định kỳ cho tất cả các game"""
    global history
    for g in GAMES:
        history[g] = load(f"luck8-{g.lower()}")
    def poll_loop(gid):
        while True:
            try:
                fetch_and_process(gid)
            except Exception as e:
                logger.error(f"❌ Lỗi trong poll_loop {gid}: {e}")
            time.sleep(POLL_INTERVAL)

    for g in GAMES:
        threading.Thread(target=poll_loop, args=(g,), daemon=True).start()


def make_handlers(gid):
    def get_latest():
        with lock:
            return jsonify(latest_payload(latest[gid], build_cau(history.get(gid, []))))

    def get_history():
        with lock:
            return jsonify({"history": decorate_history(history.get(gid, [])), "total": len(history.get(gid, []))})

    def get_history_n(n):
        if n <= 0:
            n = 1
        with lock:
            lst = history.get(gid, [])
            return jsonify({"history": decorate_history(lst, n), "total": len(lst)})

    return get_latest, get_history, get_history_n


for g in GAMES:
    path = g.lower()
    get_latest, get_history, get_history_n = make_handlers(g)
    bp.add_url_rule(f"/luck8/api/{path}", endpoint=f"get_{path}", view_func=get_latest, methods=["GET"])
    bp.add_url_rule(f"/luck8/api/{path}/history", endpoint=f"get_{path}_history", view_func=get_history, methods=["GET"])
    bp.add_url_rule(f"/luck8/api/{path}/history/<int:n>", endpoint=f"get_{path}_history_n", view_func=get_history_n, methods=["GET"])


@bp.route('/luck8/api/txmd5', methods=['GET'])
def get_txmd5():
    """Endpoint alias cho Tài Xỉu MD5"""
    with lock:
        return jsonify(latest_payload(latest["TaixiuMd5"], build_cau(history.get("TaixiuMd5", []))))


@bp.route('/luck8/api/txmd5/history', methods=['GET'])
def get_txmd5_history():
    """Endpoint lịch sử alias cho Tài Xỉu MD5"""
    with lock:
        lst = history.get("TaixiuMd5", [])
        return jsonify({"history": decorate_history(lst), "total": len(lst)})


@bp.route('/luck8/', methods=['GET'])
def index():
    """Trang chủ liệt kê các endpoint"""
    return jsonify({
        "name": "Luck8 Lottery API",
        "endpoints": [f"/luck8/api/{g.lower()}" for g in GAMES],
        "thoi_gian": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    })



def start_background():
    start_fetching()
